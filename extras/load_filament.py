import logging
from functools import partial
import typing


class LoadFilamentError(Exception):
    """Raised when there is an error loading filament"""

    def __init__(self, message, errors):
        super(LoadFilamentError, self).__init__(message)
        self.errors = errors


class LoadFilament:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")
        self.name = config.get_name().split()[-1]
        self.gcode_macro = self.printer.load_object(config, "gcode_macro")

        # * Module control variables
        self.idex_object = None
        self.cutter_object = self.cutter_name = None
        self.filament_switch_sensor_name = self.filament_switch_sensor_object = None
        self.filament_flow_sensor_name = self.filament_flow_sensor_object = None
        self.load_started: bool = False
        self.current_purge_index: int = 0
        self.travel_speed = None
        self._old_extrude_distance: float | None = None

        # * Register Event handlers
        self.printer.register_event_handler("klippy:connect", self.handle_connect)
        self.printer.register_event_handler("klippy:ready", self.handle_ready)

        # * Module Configs
        self.idex = config.getboolean("idex", False)
        self.has_custom_boundary = config.getboolean("has_custom_boundary", False)
        self.cutter_handles_rest = config.getboolean(
            "cutter_handles_rest", default=False
        )

        if config.get("filament_flow_sensor_name", None) is not None:
            self.filament_flow_sensor_name = config.get("filament_flow_sensor_name")
        if config.get("filament_switch_sensor_name", None) is not None:
            self.filament_switch_sensor_name = config.get("filament_switch_sensor_name")
        if config.get("cutter_name", None) is not None:
            self.cutter_name = config.get("cutter_name")

        self.min_dist_to_nozzle = config.getfloat(
            "minimum_distance_to_nozzle", 10.0, minval=0.1, maxval=5000.0
        )
        self.park = config.getfloatlist("park_xy", None, count=2)
        self.bucket_position = config.getfloatlist("bucket_position", count=2)
        self.extruder_to_nozzle_dist = config.getfloat(
            "extruder_to_nozzle_dist", default=30.0, minval=5.0, maxval=1000.0
        )
        self.travel_speed = config.getfloat(
            "travel_speed", default=50.0, minval=20.0, maxval=500.0
        )
        self.extrude_speed = config.getfloat(
            "extrude_speed", default=10.0, minval=5.0, maxval=100.0
        )
        self.purge_speed = config.getfloat(
            "purge_speed", default=5.0, minval=2.0, maxval=50.0
        )
        self.purge_distance = config.getfloat(
            "purge_distance", default=1.5, minval=0.5, maxval=20.0
        )
        self.purge_max_retries = config.getint(
            "purge_max_count", default=10, minval=2, maxval=30
        )
        self.purge_interval = config.getfloat(
            "purge_interval", default=3.0, minval=0.5, maxval=10.0
        )

        # * Callback Timers
        self.extrude_purge_timer = self.reactor.register_timer(
            self.purge_extrude, self.reactor.NEVER
        )
        self.extrude_to_sensor_timer = self.reactor.register_timer(
            self.extrude_to_sensor, self.reactor.NEVER
        )
        self.verify_flow_sensor_timer = self.reactor.register_timer(
            self.verify_flow_sensor_state, self.reactor.NEVER
        )
        self.verify_switch_sensor_timer = self.reactor.register_timer(
            self.verify_switch_sensor_state, self.reactor.NEVER
        )

        # * Event handlers
        if self.cutter_name is not None:
            self.printer.register_event_handler(
                "cutter_sensor:filament_present", self.handle_cutter_filament_present
            )
            self.printer.register_event_handler(
                "cutter_sensor:no_filament", self.handle_cutter_no_filament
            )

        # * Register new gcode commands
        self.gcode.register_mux_command(
            "LOAD_FILAMENT",
            "TOOLHEAD",
            self.name,
            self.cmd_LOAD_FILAMENT,
            "GCODE MACRO to load filament, takes into account if there is a belay and or a filament cutter with a sensor.",
        )
        self.gcode.register_mux_command(
            "PURGE_STOP",
            "TOOLHEAD",
            self.name,
            self.cmd_PURGE_STOP,
            "Helper gcode command that stop filament purging",
        )
        self.gcode.register_mux_command(
            "GET_GCODEMOVE",
            "TOOLHEAD",
            self.name,
            self.cmd_GET_GCODEMOVE,
            "get gcode move ",
        )

    def cmd_GET_GCODEMOVE(self, gcmd):
        gcode_move = self.printer.lookup_object("gcode_move")
        logging.info(gcode_move.get_status(self.reactor.monotonic()))

    def handle_connect(self):
        self.toolhead = self.printer.lookup_object("toolhead")

    def handle_ready(self):
        self.min_event_systime = self.reactor.monotonic() + 2.0
        if self.cutter_name is not None:
            self.cutter_object = self.printer.lookup_object(
                f"cutter_sensor {self.cutter_name}"
            )
        if self.idex:
            self.idex_object = self.printer.lookup_object("dual_carriage")
        if self.filament_flow_sensor_name is not None:
            self.filament_flow_sensor_object = self.printer.lookup_object(
                f"filament_motion_sensor {self.filament_flow_sensor_name}"
            )
        if self.filament_switch_sensor_name is not None:
            self.filament_switch_sensor_object = self.printer.lookup_object(
                f"filament_switch_sensor {self.filament_switch_sensor_name}"
            )
        if self.has_custom_boundary:
            self.custom_boundary_object = self.printer.lookup_object("bed_custom_bound")

    def handle_cutter_filament_present(self, eventtime):
        if self.load_started:
            self.reactor.update_timer(self.extrude_to_sensor_timer, self.reactor.NEVER)
            self.toolhead.wait_moves()
            self.reactor.update_timer(self.extrude_purge_timer, self.reactor.NOW)

    def handle_cutter_no_filament(self, eventtime):
        if self.load_started:
            self.gcode.respond_info("Purge start.")
            self.reactor.update_timer(self.extrude_purge_timer, self.reactor.NOW)
            self.change_extrude_dist(self._old_extrude_distance)

    def verify_switch_sensor_state(self, eventtime):
        if self.load_started and self.filament_switch_sensor_object is not None:
            if self.filament_switch_sensor_object.get_status(eventtime)[
                "filament_detected"
            ]:
                self.reactor.update_timer(
                    self.extrude_to_sensor_timer, self.reactor.NEVER
                )
                self.move_extruder_mm(
                    self.extruder_to_nozzle_dist, speed=30, wait=True
                )  # Extrude to nozzle
                self.reactor.update_timer(self.extrude_purge_timer, self.reactor.NOW)
                return self.reactor.NEVER
        return eventtime + 2.375

    def verify_flow_sensor_state(self, eventtime):
        if self.load_started and self.filament_flow_sensor_object is not None:
            if self.filament_flow_sensor_object.runout_helper.get_status(eventtime)[
                "filament_detected"
            ]:
                self.reactor.update_timer(
                    self.extrude_to_sensor_timer, self.reactor.NEVER
                )
                self.move_extruder_mm(
                    self.extruder_to_nozzle_dist, speed=30, wait=True
                )  # Extrude to nozzle
                self.reactor.update_timer(self.extrude_purge_timer, self.reactor.NOW)
                return self.reactor.NEVER
            return eventtime + 0.775

    def extrude_to_sensor(self, eventtime):
        if self.load_started and (
            self.filament_flow_sensor_object is not None
            or self.filament_switch_sensor_object is not None
            or self.cutter_object is not None
        ):
            self.move_extruder_mm(distance=10, speed=self.extrude_speed, wait=False)
            return eventtime + float((10 / self.extrude_speed))

    def purge_extrude(self, eventtime):
        if self.current_purge_index > self.purge_max_retries:
            self.gcode.respond_info("Load routine ended.")
            completion = self.reactor.register_callback(self._purge_end)
            completion.wait()
            return self.reactor.NEVER
        self.move_extruder_mm(distance=self.purge_distance, speed=self.purge_speed)
        self.current_purge_index += 1
        return eventtime + float(self.purge_interval)

    def _purge_end(self, eventtime):
        self.reactor.update_timer(self.extrude_purge_timer, self.reactor.NEVER)

        if self.park is not None:
            self.toolhead.manual_move([self.park[0], self.park[1]], self.travel_speed)
            self.toolhead.wait_moves()

        if self.has_custom_boundary and self.custom_boundary_object is not None:
            if self.custom_boundary_object.get_status()["status"] == "default":
                # * Restore the boundary to the custom one
                self.custom_boundary_object.set_custom_boundary()

        self.change_extrude_dist(self._old_extrude_distance)
        self._old_extrude_distance = None
        self.current_purge_index = 0
        self.load_started = False
        self.printer.send_event("load_filament:end")
        
        self.toolhead.wait_moves()
        
        self.gcode.run_script_from_command("G90\nM400")
        self.gcode.run_script_from_command("M83\nM400")
        self.gcode.run_script_from_command("G92 E0.0\nM400")

            # self.toolhead.commanded_pos[3] = 0.0
        # prev_pos = self.toolhead.get_position()
        # self.toolhead.set_position((prev_pos[0], prev_pos[1], prev_pos[2], 0.0),homing_axes=(0, 1, 2) )

        self.gcode.respond_info("Restored positions.")
        self.restore_state()

        # resume_completion = self.reactor.register_callback(self.conditional_resume)
        # resume_completion.wait()
        self.heat_and_wait(0, wait=False)
        self.gcode.run_script_from_command("T0 PARK\nM400")

    def move_extruder_mm(self, distance=10.0, speed=30.0, wait=True):
        """Move the extruder

        Args:
            distance (float): The distance in mm to move the extruder.
        """
        try:
            eventtime = self.reactor.monotonic()
            gcode_move = self.printer.lookup_object("gcode_move")
            prev_pos = self.toolhead.get_position()
            v = distance * gcode_move.get_status(eventtime)["extrude_factor"]
            new_distance = v + prev_pos[3]
            self.toolhead.move(
                [prev_pos[0], prev_pos[1], prev_pos[2], new_distance], speed
            )
            if wait:
                self.toolhead.wait_moves()
        except Exception:
            logging.error("Unexpected error while trying to move extruder.")
            return False
        return True

    def move_to_bucket(self, split: typing.Optional["bool"] = False):
        """Moves to bucket position"""
        if self.toolhead is None:
            return
        if not split:
            if self.bucket_position[1] == -999999:
                self.toolhead.manual_move([self.bucket_position[0]], self.travel_speed)
            else:
                self.toolhead.manual_move(
                    [self.bucket_position[0], self.bucket_position[1]],
                    self.travel_speed,
                )
        else:
            if self.bucket_position[1] == -999999:
                self.toolhead.manual_move([self.bucket_position[0]], self.travel_speed)
                self.toolhead.wait_moves()
            else:
                self.toolhead.manual_move([self.bucket_position[0]], self.travel_speed)
                self.toolhead.wait_moves()
                self.toolhead.manual_move([self.bucket_position[1]], self.travel_speed)
        self.toolhead.wait_moves()

    def move_to_home_pos(self):
        """Move the toolhead to the home position (To the park position)"""
        if self.toolhead is None:
            return
        self.toolhead.manual_move([self.park[0], self.park[1]], self.travel_speed)
        self.toolhead.wait_moves()

    def home_if_needed(self):
        """Perform home if needed"""
        if self.toolhead is None:
            return
        try:
            eventtime = self.reactor.monotonic()
            kin = self.toolhead.get_kinematics()
            _homed_axes = kin.get_status(eventtime)["homed_axes"]
            if "xyz" in _homed_axes.lower():
                return
            else:
                self.gcode.respond_info("Homing")
                self.gcode.run_script_from_command("G28")

        except Exception as e:
            logging.error(f"Unable to perform home on load filament, error: {e}")

    def heat_and_wait(self, temp, wait: typing.Optional["bool"] = False):
        """Heats the extruder and wait.

        Method returns when  temperature is [temp - 5 ; temp + 5].
        Args:
            temp (float):
                Target temperature in Celsius.
            wait (bool, optional):
                Weather to wait or not for the temperature to reach the interval . Defaults to True
        """
        eventtime = self.reactor.monotonic()
        extruder = self.toolhead.get_extruder()
        pheaters = self.printer.lookup_object("heaters")
        pheaters.set_temperature(extruder.get_heater(), temp, False)

        extruder_heater = extruder.get_heater()

        while not self.printer.is_shutdown() and wait:
            heater_temp, target = extruder_heater.get_temp(eventtime)
            if heater_temp >= (temp - 5) and heater_temp <= (temp + 5):
                return
            eventtime = self.reactor.pause(eventtime + 1.0)

    def _exec_gcode(self, template):
        """Run a Gcode command"""
        try:
            self.gcode.run_script(template.render() + "\nM400")
        except Exception:
            logging.exception("Error running gcode script on load_filament.py")
        self.min_event_systime = self.reactor.monotonic() + 2.0

    def increase_extrude_dist(self) -> float:
        """
        Increase current extruder config `max_extrude_only_distance` to the configured `minimum_extrude_dist`.

        Returns:
            float: The old `max_extrude_only_distance` as set on `[extruder]` config.
        """
        # * Increase the max extrude distance if needed
        extruder = self.toolhead.get_extruder()
        _old_extruder_dist = None
        if extruder.max_e_dist < self.min_dist_to_nozzle:
            _old_extruder_dist = extruder.max_e_dist
            extruder.max_e_dist = self.min_dist_to_nozzle + 10.0
            return _old_extruder_dist
        return None

    def change_extrude_dist(self, extrude_dist):
        """
        Changes the `max_e_dist` variable of the current extruder object.

        Args:
            extrude_dist (float): The new value for the variable `max_e_dist` on the extruder object.
        """
        if extrude_dist is not None:
            extruder = self.toolhead.get_extruder()
            if extruder is not None:
                extruder.max_e_dist = float(extrude_dist)

    def disable_sensors(self):
        if self.filament_flow_sensor_object is not None:
            self.filament_flow_sensor_object.runout_helper.sensor_enabled = 0

        if self.filament_switch_sensor_object is not None:
            self.filament_switch_sensor_object.sensor_enabled = 0

    def enable_sensors(self, eventtime):
        if self.filament_flow_sensor_object is not None:
            self.filament_flow_sensor_object.runout_helper.sensor_enabled = 1

        if self.filament_switch_sensor_object is not None:
            self.filament_switch_sensor_object.sensor_enabled = 1

    def conditional_pause(self, eventtime):
        idle_timeout = self.printer.lookup_object("idle_timeout")
        pause_resume = self.printer.lookup_object("pause_resume")
        virtual_sdcard = self.printer.lookup_object("virtual_sdcard")

        if idle_timeout is None or pause_resume is None:
            return None

        is_printing = idle_timeout.get_status(eventtime)["state"] == "Printing"
        is_paused = pause_resume.get_status(eventtime)["is_paused"]
        has_file = virtual_sdcard.is_active()

        if is_printing and not is_paused and self.load_started and has_file:
            if self.printer.lookup_object("gcode_macro PAUSE") is not None:
                self.gcode.run_script_from_command("PAUSE")
        return False

    def conditional_resume(self, eventtime):
        idle_timeout = self.printer.lookup_object("idle_timeout")
        pause_resume = self.printer.lookup_object("pause_resume")
        virtual_sdcard = self.printer.lookup_object("virtual_sdcard")

        if idle_timeout is None or pause_resume is None:
            return None

        is_printing = idle_timeout.get_status(eventtime)["state"] == "Printing"
        is_paused = pause_resume.get_status(eventtime)["is_paused"]
        has_file = virtual_sdcard.is_active()
        
        if is_paused and not is_printing and self.load_started and has_file:
            if self.printer.lookup_object("gcode_macro RESUME") is not None:
                self.gcode.run_script_from_command("RESUME")

        return False

    def fix_extruder(self):
        eventtime = self.reactor.monotonic()
        gcode_move = self.printer.lookup_object("gcode_move")
        gcode_move.base_position[3] = 0.0
        gcode_move.last_position[3] = 0.0

    def save_state(self):
        """Save gcode state and dual carriage state if the system is in IDEX configuration"""
        if self.idex:
            self.gcode.run_script_from_command(
                f"SAVE_DUAL_CARRIAGE_STATE NAME=load_carriage_state_{self.name}\nM400"
            )
        self.gcode.run_script_from_command(f"SAVE_GCODE_STATE NAME=load_state_{self.name}\nM400")
        return True

    def restore_state(self):
        """Restore gcode state and dual carriage state if the system is in IDEX configuration"""
        self.gcode.run_script_from_command(f"RESTORE_GCODE_STATE NAME=load_state_{self.name} \nM400")
        if self.idex:
            self.gcode.run_script_from_command(
                f"RESTORE_DUAL_CARRIAGE_STATE NAME=load_carriage_state_{self.name} MOVE=0\nM400"
            )
        return True

    ####################################################################################################################
    ##################################################### GCODE COMMANDS ###############################################
    ####################################################################################################################
    def cmd_PURGE_STOP(self, gcmd):
        self._purge_end()

    def cmd_LOAD_FILAMENT(self, gcmd):
        temp = gcmd.get("TEMPERATURE", 220.0, parser=float, minval=210, maxval=250)
        try:
            # pause_completion = self.reactor.register_callback(self.conditional_pause)
            # pause_completion.wait()

            self.save_state()

            if gcmd.get("TOOLHEAD") == "Load_T0": 
                self.gcode.run_script_from_command("T0 LOAD")
            else: 
                self.gcode.run_script_from_command("T1 LOAD")

    
            self.disable_sensors()
            self.load_started = True
            self.printer.send_event("load_filament:start")
            self.gcode.respond_info("Start load filament.")

            self.gcode.run_script_from_command("G90\nM400")
            self.gcode.run_script_from_command("M83\nM400")
            self.gcode.run_script_from_command("G92 E0.0\nM400")

            self.home_if_needed()

            if self.has_custom_boundary:
                self.custom_boundary_object.restore_default_boundary()

            self.heat_and_wait(temp, wait=False)
            self.move_to_bucket()
            self.heat_and_wait(temp, wait=True)
            self.toolhead.wait_moves()

            # * Increase max extrude distance if needed
            self._old_extrude_distance = self.increase_extrude_dist()

            # * Force the motion sensor to "No Filament" state
            if self.filament_flow_sensor_object is not None:
                self.reactor.register_callback(
                    partial(self.filament_flow_sensor_object.encoder_event, state=False)
                )
                self.move_extruder_mm(distance=30, speed=40, wait=True)

            if self.filament_switch_sensor_object is not None:
                self.filament_switch_sensor_object.note_filament_present(False)
                self.move_extruder_mm(distance=30, speed=40, wait=True)

            

            self.reactor.update_timer(
                self.extrude_to_sensor_timer, self.reactor.NOW
            )  # Start extrusion

            if self.filament_flow_sensor_object is not None:
                self.reactor.update_timer(
                    self.verify_flow_sensor_timer, self.reactor.NOW
                )

            if self.filament_switch_sensor_object is not None:
                self.reactor.update_timer(
                    self.verify_switch_sensor_timer, self.reactor.NOW
                )

            # * Restore extruder min extrude distance if other objects don't handle the load filament procedure
            if (
                isinstance(self._old_extrude_distance, float)
                and not self.cutter_handles_rest
                and self.cutter_object is None
                and self.filament_flow_sensor_object is None
                and self.filament_switch_sensor_object is None
            ):
                self.toolhead.wait_moves()
                self.change_extrude_dist(self._old_extrude_distance)
                self.enable_sensors()
                self.restore_state()

                resume_completion = self.reactor.register_callback(
                    self.conditional_resume
                )
                resume_completion.wait()

                self.printer.send_event("load_filament:end")
        except LoadFilamentError as e:
            logging.error(f"Error loading filament : {e}")


def load_config_prefix(config):
    return LoadFilament(config)
