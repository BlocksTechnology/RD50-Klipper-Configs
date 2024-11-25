import logging
import typing


class UnloadFilamentError(Exception):
    """Raised when there is an error unloading filament"""

    def __init__(self, message, errors):
        super(UnloadFilamentError, self).__init__(message)
        self.errors = errors


class UnloadFilament:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.name = config.get_name().split()[-1]
        self.gcode = self.printer.lookup_object("gcode")
        self.gcode_macro = self.printer.load_object(config, "gcode_macro")

        self.idex_object = None
        self._old_extrude_dist = None
        self.cutter_object = self.cutter_name = None
        self.custom_boundary_object = None
        self.min_event_systime = None
        self.toolhead = None
        self.bucket_position = None
        self.filament_flow_sensor_object = self.filament_flow_sensor_name = None
        self.filament_switch_sensor_object = self.filament_switch_sensor_name = None
        self.unload_started: bool = None
        self.pre_unload_gcode = self.post_unload_gcode = None
        self.travel_speed = None

        # * Register Event handlers
        self.printer.register_event_handler("klippy:connect", self.handle_connect)
        self.printer.register_event_handler("klippy:ready", self.handle_ready)

        # * Module Configs
        self.idex = config.getboolean("idex", False)
        self.has_custom_boundary = config.getboolean("has_custom_boundary", False)
        self.travel_speed = config.getfloat(
            "travel_speed", 100.0, minval=50.0, maxval=500.0
        )
        if config.getfloatlist("bucket", None, count=2) is not None:
            self.bucket_position = config.getfloatlist("bucket", count=2)

        if config.get("filament_flow_sensor_name", None) is not None:
            self.filament_flow_sensor_name = config.get("filament_flow_sensor_name")

        if config.get("filament_switch_sensor_name", None) is not None:
            self.filament_switch_sensor_name = config.get("filament_switch_sensor_name")

        if config.get("cutter_name", None) is not None:
            self.cutter_name = config.get("cutter_name")

        self.min_dist_to_nozzle = config.getfloat(
            "minimum_dist_to_nozzle", default=30.0, minval=20.0, maxval=3000.0
        )

        self.park = config.getfloatlist("park_xy", None, count=2)

        self.extrude_speed = config.getfloat(
            "extrude_speed", default=10.0, minval=5.0, maxval=50.0
        )

        # * Callback Timers
        self.unextrude_timer = self.reactor.register_timer(
            self.unextrude, self.reactor.NEVER
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
                "cutter_sensor:no_filament", self.handle_cutter_fnp
            )

        # * Register new Gcode command
        self.gcode.register_mux_command(
            "UNLOAD_FILAMENT",
            "TOOLHEAD",
            self.name,
            self.cmd_UNLOAD_FILAMENT,
            "GCODE Macro to unload filament, takes into account if there is a belay and or a filament cutter with a sensor",
        )

    def handle_connect(self):
        self.toolhead = self.printer.lookup_object("toolhead")

    def handle_ready(self):
        self.min_event_systime = self.reactor.monotonic() + 2.0

        if self.has_custom_boundary:
            self.custom_boundary_object = self.printer.lookup_object("bed_custom_bound")

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

    def handle_cutter_fnp(self, eventtime):
        if self.unload_started and self.cutter_object is not None:
            self.reactor.update_timer(self.unextrude_timer, self.reactor.NOW)
            self.toolhead.wait_moves()

    def verify_switch_sensor_state(self, eventtime):
        if self.unload_started and self.filament_switch_sensor_object is not None:
            if self.filament_switch_sensor_object.get_status(eventtime)[
                "filament_detected"
            ]:
                return eventtime + 1.275
            else:
                self.gcode.respond_info("Unload finished.")
                self.reactor.update_timer(self.unextrude_timer, self.reactor.NEVER)

                self.move_extruder_mm(20, speed=15, wait=True)
                self.toolhead.wait_moves()

                if self._old_extrude_dist is not None:
                    self.change_extrude_dist(self._old_extrude_dist)
                
                
                self.toolhead.wait_moves()

                self.gcode.run_script_from_command("G90\nM400")
                self.gcode.run_script_from_command("M83\nM400")
                self.gcode.run_script_from_command("G92 E0.0\nM400")

                self.heat_and_wait(0, wait=False)
                
                self.gcode.run_script_from_command("T0 PARK\nM400")
                self.unload_started = False
                self.printer.send_event("unload_filament:end")
                self.toolhead.wait_moves()
                
                self.restore_state()

                return self.reactor.NEVER
        return eventtime + 2.750

    def verify_flow_sensor_state(self, eventtime):
        if self.unload_started and self.filament_flow_sensor_object is not None:
            if self.filament_flow_sensor_object.runout_helper.get_status(eventtime)[
                "filament_detected"
            ]:
                return eventtime + 1.0
            else:
                return self.reactor.NEVER
        return eventtime + 2.5

    def unextrude(self, eventtime):
        """Move the extruder to unload"""
        self.move_extruder_mm(distance=-10, speed=self.extrude_speed, wait=False)
        return eventtime + float((10 / self.extrude_speed))

    def disable_sensors(self):
        if self.filament_flow_sensor_object is not None:
            self.filament_flow_sensor_object.runout_helper.sensor_enabled = 0

        if self.filament_switch_sensor_object is not None:
            self.filament_switch_sensor_object.sensor_enabled = 0

    def enable_sensors(self):
        if self.filament_flow_sensor_object is not None:
            self.filament_flow_sensor_object.runout_helper.sensor_enabled = 1

        if self.filament_switch_sensor_object is not None:
            self.filament_switch_sensor_object.sensor_enabled = 1

    def move_extruder_mm(self, distance=10.0, speed=30.0, wait=True):
        """Move the extruder

        Args:
            distance (float): The distance in mm to move the extruder.
        """
        if self.toolhead is None:
            return
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
        except Exception as e:
            logging.error(f"Unexpected error while trying to move extruder: {e} ")
            return False
        return True

    def home_if_needed(self):
        if self.toolhead is None:
            return
        try:
            eventtime = self.reactor.monotonic()
            kin = self.toolhead.get_kinematics()
            _homed_axes = kin.get_status(eventtime)["homed_axes"]
            if "xyz" in _homed_axes.lower():
                return
            else:
                self.gcode.run_script_from_command("G28")
        except Exception as e:
            logging.error(f"Unable to home for somereason on load filament: {e}")

    def _exec_gcode(self, template):
        try:
            self.gcode.run_script(template.render() + "\nM400")
        except Exception:
            logging.exception("Error running gcode script on load_filament.py")
        self.min_event_systime = self.reactor.monotonic() + 2.0

    def heat_and_wait(self, temp, wait: typing.Optional["bool"] = False):
        """Heats the extruder and wait.

        Method returns when  temperature is [temp - 5 ; temp + 5].
        Args:
            temp (float):
                Target temperature in Celsios.
            wait (bool, optional):
                Weather to wait or not for the temperature to reach the interval . Defaults to True
        """

        eventtime = self.reactor.monotonic()
        extruder = self.toolhead.get_extruder()
        pheaters = self.printer.lookup_object("heaters")
        pheaters.set_temperature(extruder.get_heater(), temp, False)
        extruder_heater = extruder.get_heater()
        while not self.printer.is_shutdown() and wait:
            self.gcode.respond_info("Waiting for temperature to stabilize.")
            heater_temp, target = extruder_heater.get_temp(eventtime)
            if heater_temp >= (temp - 5) and heater_temp <= (temp + 5):
                return

            eventtime = self.reactor.pause(eventtime + 1.0)

    def move_to_bucket(self, split: typing.Optional["bool"] = False):
        """Moves to bucket position"""
        if self.toolhead is None:
            return
        # * Maybe check if the move is within the printers boundaries
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
                extruder.max_e_dist = extrude_dist

    def save_state(self):
        """Save gcode state and dual carriage state if the system is in IDEX configuration"""
        if self.idex:
            self.gcode.run_script_from_command(
                "SAVE_DUAL_CARRIAGE_STATE NAME=unload_carriage_state\nM400"
            )
        self.gcode.run_script_from_command("SAVE_GCODE_STATE NAME=unload_state\nM400")
        self.toolhead.wait_moves()

        return True

    def restore_state(self):
        """Restore gcode state and dual carriage state if the system is in IDEX configuration"""
        self.gcode.run_script_from_command("RESTORE_GCODE_STATE NAME=unload_state MOVE=0\nM400")
        if self.idex:
            self.gcode.run_script_from_command(
                "RESTORE_DUAL_CARRIAGE_STATE NAME=unload_carriage_state MOVE=0\nM400"
            )
        self.toolhead.wait_moves()

        return True
    ####################################################################################################################
    ##################################################### GCODE COMMANDS ###############################################
    ####################################################################################################################
    def cmd_UNLOAD_FILAMENT(self, gcmd):
        temp = gcmd.get("TEMPERATURE", 250.0, parser=float, minval=210.0, maxval=260.0)
        if self.toolhead is None:
            return
        try:
            self.save_state()
            
            if gcmd.get("TOOLHEAD") == "Unload_T0": 
                self.gcode.run_script_from_command("T0 UNLOAD")
            else: 
                self.gcode.run_script_from_command("T1 UNLOAD")

            self.toolhead.wait_moves()
            self.disable_sensors()
            
            self.unload_started = True
            self.printer.send_event("unload_filament:start")
            self.heat_and_wait(temp, wait=False)
            self.home_if_needed()
            self.move_to_bucket()
            self.heat_and_wait(temp, wait=True)
            self._old_extrude_dist = self.increase_extrude_dist()
            self.gcode.run_script_from_command("G90\nM400")
            self.gcode.run_script_from_command("M83\nM400")
            self.gcode.run_script_from_command("G92 E0.0\nM400")

            self.toolhead.wait_moves()

            self.move_extruder_mm(
                distance=-30, speed=40, wait=True
            )  # Fast retract Tip forming

            self.reactor.update_timer(self.unextrude_timer, self.reactor.NOW)
            self.toolhead.wait_moves()
            if self.cutter_object is not None:
                pass
            if self.filament_flow_sensor_object is not None:
                self.reactor.update_timer(
                    self.verify_flow_sensor_timer, self.reactor.NOW
                )
            if self.filament_switch_sensor_object is not None:
                self.reactor.update_timer(
                    self.verify_switch_sensor_timer, self.reactor.NOW
                )
            self.toolhead.wait_moves()
            if (
                self.filament_flow_sensor_object is None
                and self.filament_switch_sensor_object is None
                and self.cutter_object is None
            ):
                if self._old_extrude_dist is not None:
                    self.change_extrude_dist(self._old_extrude_dist)
                self.restore_state()
                self.toolhead.wait_moves()
                self.gcode.run_script_from_command("G90\nM400")
                self.gcode.run_script_from_command("M83\nM400")
                self.gcode.run_script_from_command("G92 E0.0\nM400")

                self.enable_sensors()
                self.printer.send_event("unload_filament:end")
        except Exception as e:
            logging.error(f"Unexpected error while trying to unload filament: {e}.")


def load_config_prefix(config):
    return UnloadFilament(config)
