import logging
import typing
from functools import partial


class CleanNozzle:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")
        self.name = config.get_name().split()[1]

        self.toolhead = None

        self.printer.register_event_handler("klippy:connect", self.handle_connect)

        # * Needed parameters for this module
        self.iterations = config.getint("iterations", default=3, minval=1, maxval=20)
        self.travel_speed = config.getfloat(
            "travel_speed", default=50.0, minval=20.0, maxval=300.0
        )
        self.toolhead_number = config.getint(
            "toolhead_count", default=1, minval=1, maxval=2
        )

        self.clean_speed = (
            config.getfloat("clean_speed", default=50.0, minval=20.0, maxval=10000.0)
            * 60
        )

        self.wiper_pos_x = config.getfloatlist("wiper_pos_x", None, count=2)
        self.wiper_pos_y = config.getfloatlist("wiper_pos_y", None, count=2)

        if self.toolhead_number == 2:
            self.wiper_pos_x_2 = config.getfloatlist("wiper_pos_x_2", None, count=2)
            self.wiper_pos_y_2 = config.getfloatlist("wiper_pos_y_2", None, count=2)

        # * Register gcode commands
        self.gcode.register_mux_command(
            "_CLEAN_NOZZLE",
            "CONFIG",
            self.name,
            self.cmd_CLEAN_NOZZLE,
            "Routine that cleans the nozzle, by ",
        )

    def handle_connect(self):
        self.toolhead = self.printer.lookup_object("toolhead")

    def cmd_CLEAN_NOZZLE(self, gcmd):
        temp = gcmd.get(
            "TEMPERATURE", default=220.0, parser=float, minval=180.0, maxval=300.0
        )
        target_head = gcmd.get_int("TARGET_HEAD", 0, minval=0, maxval=2)
        park = gcmd.get("PARK", default=False, parser=bool)

        self.home_if_needed()

        self.heat_and_wait(temp, head=target_head, wait=True)

        self.activate_carriage(index=target_head)

        completion = self.reactor.register_callback(
            partial(self._clean_nozzle, target=target_head)
        )
        completion.wait()

        # * Cooldown heaters / select the primary toolhead again
        self.heat_and_wait(temp=0, head=target_head, wait=False)
        # self.activate_carriage(index=0)

        if park:
            self.park()

    def _clean_nozzle(self, eventtime, target: int):
        if self.toolhead is None:
            return

        try:
            self.gcode.respond_info(
                f"Initiating cleaning procedure on target {target}."
            )

            self.move_to_initial_pos(head=target, initial=True)
            for iter in range(0, self.iterations):
                self.move_to_final_pos(head=target)
                self.move_to_initial_pos(head=target, initial=False)
                iter += 1

            return True

        except Exception as e:
            self.gcode.respond_info(
                "Exception occurred, unable to clean nozzle printer shutdown."
            )
            logging.info(f"Unable to clean nozzle errors: {e}")

    def park(self):
        pass

    def move_to_initial_pos(self, head: int = 0, initial: bool = True):
        if self.toolhead is None:
            return
        try:
            if initial:
                if head == 0 or head == 2:
                    self.toolhead.manual_move(
                        [self.wiper_pos_x[0], self.wiper_pos_y[0]], self.travel_speed
                    )
                if self.toolhead_number == 2 and (head == 1 or head == 2):
                    self.toolhead.manual_move(
                        [self.wiper_pos_x_2[0], self.wiper_pos_y_2[0]],
                        self.travel_speed,
                    )
            elif not initial:
                if head == 0 or head == 2:
                    self.toolhead.manual_move(
                        [self.wiper_pos_x[0], self.wiper_pos_y[0]], self.clean_speed
                    )

                if self.toolhead_number == 2 and (head == 1 or head == 2):
                    self.toolhead.manual_move(
                        [self.wiper_pos_x_2[0], self.wiper_pos_y_2[0]], self.clean_speed
                    )

            return
        except Exception as e:
            logging.info(
                f"Exception occurred when moving to the initial wipe position. {e}"
            )
            return

    def move_to_final_pos(self, head: int = 0):
        if self.toolhead is None:
            return

        try:
            if head == 0 or head == 2:
                self.toolhead.manual_move(
                    [self.wiper_pos_x[1], self.wiper_pos_y[1]], self.clean_speed
                )
            if self.toolhead_number == 2 and (head == 1 or head == 2):
                self.toolhead.manual_move(
                    [self.wiper_pos_x_2[1], self.wiper_pos_y_2[1]], self.clean_speed
                )

            return
        except Exception as e:
            logging.info(
                f"Exception occurred when moving to the initial wipe position. {e}"
            )
            return

    def activate_carriage(self, index):
        if self.toolhead is None:
            return

        if index > 2:
            self.gcode.respond_info(
                f"No Toolhead with index {index}, cannot activate the extruder."
            )
            return
        try:
            # * Activate the extruder on the toolhead
            _prefix = "extruder"
            dual_carriage = self.printer.lookup_object("dual_carriage")
            self.gcode.respond_info(f"Activating extruder {_prefix}{index}")

            if index == 0:
                dual_carriage.activate_dc_mode(0, "PRIMARY")
                dual_carriage.activate_dc_mode(0)
            if index == 1 and self.toolhead_number == 2:
                dual_carriage.activate_dc_move(1)
            if index == 2 and self.toolhead_number == 2:
                dual_carriage.activate_dc_mode(0, "PRIMARY")
                dual_carriage.activate_dc_mode(1, "MIRROR")
            return
        except Exception as e:
            logging.info(f"Exception occurred. Unable to activate head: {e}")

    def heat_and_wait(
        self, temp, wait: typing.Optional["bool"] = True, head: int = [0, 1, 2]
    ):
        """Heats the extruder and wait.

        Method returns when  temperature is [temp - 5 ; temp + 5].
        Args:
            temp (float):
                Target temperature in Celsius.
            wait (bool, optional):
                Weather to wait or not for the temperature to reach the interval . Defaults to True
        """
        eventtime = self.reactor.monotonic()
        _primary_extruder = self.toolhead.get_extruder()
        _secondary_extruder = self.printer.lookup_object("extruder1")

        pheaters = self.printer.lookup_object("heaters")
        if head == 0 or head == 2:
            pheaters.set_temperature(_primary_extruder.get_heater(), temp, False)
        if head == 1 or head == 2:
            pheaters.set_temperature(_secondary_extruder.get_heater(), temp, False)

        _primary_extruder_heater = _primary_extruder.get_heater()
        _secondary_extruder_heater = _secondary_extruder.get_heater()

        self.gcode.respond_info("Waiting for temperature to stabilize.")
        while not self.printer.is_shutdown() and wait:
            if head == 0:
                heater_temp, target = _primary_extruder_heater.get_temp(eventtime)
                if heater_temp >= (temp - 5) and heater_temp <= (temp + 5):
                    return
            if head == 1:
                heater_temp, target = _secondary_extruder_heater.get_temp(eventtime)
                if heater_temp >= (temp - 5) and heater_temp <= (temp + 5):
                    return
            if head == 2:
                heater_temp_1, target = _primary_extruder_heater.get_temp(eventtime)
                heater_temp_2, target = _secondary_extruder_heater.get_temp(eventtime)

                if (
                    heater_temp_1 >= (temp - 5)
                    and heater_temp_1 <= (temp + 5)
                    and heater_temp_2 >= (temp - 5)
                    and heater_temp_2 <= (temp + 5)
                ):
                    return

            eventtime = self.reactor.pause(eventtime + 1.0)

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
                self.gcode.respond_info("Homing machine.")
                # completion = self.reactor.register_callback(self._exec_gcode("G28"))
                self.gcode.run_script_from_command("G28")

            self.gcode.respond_info("Waiting for homing.")
        except Exception as e:
            logging.error(f"Unable to home for somereason on load filament: {e}")


def load_config_prefix(config):
    return CleanNozzle(config)
