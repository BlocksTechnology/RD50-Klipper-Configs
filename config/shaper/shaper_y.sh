#!/bin/bash

if [$# -eq 0]; then
  echo "Error: No generated file name provided."
  echo "Usage: $0 <Input shaper generated file name>"
  exit 1
fi
filename_arg = $1
created_dir = "/tmp/calibration_data_y_${filename_arg}.csv"
echo "$created_dir"
python3 ~/home/biqu/klipper/scripts/calibrate_shaper.py /tmp/calibration_data_y_"$filename_arg".csv -o ~/home/biqu/printer_data/config/shaper/shaper_calibrate_y.png