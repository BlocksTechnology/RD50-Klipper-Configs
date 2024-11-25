#!/usr/bin/env bash
#############################################################################################
#                                                                                           #
#                    Blocks config update installer for RD50 Machines                       #
#                                                                                           #
#############################################################################################
#############################################################################################
#   Based on Klippain, refer to https://github.com/Frix-x/klippain/blob/main/install.sh     #
#                                                                                           #
# Which was originally written by yomgui1 & Frix_x All                                      #
#############################################################################################
#############################################################################################
#                                                                                           #
#                       This script will backup and delete old configs                      #
#                            And create symlinks to new ones                                #
#                                                                                           #
#############################################################################################

umask 022
set -eu # Exit with error if any script return non-zero exit status and if variables are unset

function verify_ready() {
    if [ "$EUID" -eq 0 ]; then
        echo_error "This script must not be run as root!"
        exit 255
    fi
}

# Directories needed
USER_CONFIG_PATH="${HOME}/printer_data/config"
BACKUP_PATH="${HOME}/RD50_backup/"
RD50_Klipper_Configs_PATH="${HOME}/RD50-Klipper-Configs"
KLIPPER_PATH="${HOME}/klipper/"

BLOCKS_RD50_CONFIG_BRANCH="main"

Red='\033[0;31m'
Green='\033[0;32m'
Blue='\033[0;34m'
Cyan='\033[0;36m'
Normal='\033[0m'

function echo_info() {
    printf "%s\n" "${Blue}$1${Normal}\n"
}

function echo_text() {
    printf "%s\n" "${Normal}$1${Cyan}\n"
}

function echo_error() {
    printf "%s\n" "${Red}$1${Normal}\n"
}

function echo_ok() {
    printf "%s\n" "${Green}$1${Normal}\n"
}

function progress() {
    echo -e "\n\n###### $1"
}
# Backup, compress the files and place them in a backup directory
function backup() {
    if [ ! -e "${USER_CONFIG_PATH}" ]; then
        echo_info "[BACKUP] No previous config found, skipping backup...\n\n"
        return 1
    fi

    ## Create directory, specify the new compressed file with the configurations
    current_date=$(date +'Backup_%d-%m-%Y_%H_%M_%S')

    mkdir -p "${BACKUP_PATH}" # If the directory already exists do not overwrite it.

    if [ -d "${BACKUP_PATH}" ]; then
        echo_ok "[BACKUP] Directory at: ${BACKUP_PATH}"
    else
        echo_error "[BACKUP] Error creating directory.... Aborting"
        return 1
    fi

    ## Remove all symlink files on the users config path
    find "${USER_CONFIG_PATH}" -type l -exec rm -f {} \;

    # Compress the config directory
    echo_info "Compressing configuration files"

    zip -qr "${BACKUP_PATH}/${current_date}.zip" "${USER_CONFIG_PATH}" # TODO Progress bar: | pv -bep -s "$(du -bs "${USER_CONFIG_PATH}")" | awk '{print $1}' >"${BACKUP_PATH}${current_date}.zip"

    ## Check if the backup file exists
    if [ -f "${BACKUP_PATH}${current_date}.zip" ] && [ "$(du -bs "${BACKUP_PATH}${current_date}.zip" | cut -f1)" -gt "0" ]; then

        echo_ok "[BACKUP] Backup successful, old user config files on ${BACKUP_PATH}${current_date}.zip \n"

        ## Wipe old configurations from the klipper config path
        echo_info "Wiping old configuration files on the user config directory..."
        rm -rf "${USER_CONFIG_PATH:?}"/{*,.*} # Delete all files and folders on this directory and hidden files as well

        # Check if the directory is clean
        if [ ! "$(ls -A "${USER_CONFIG_PATH}")" ]; then
            echo_ok "Successfully Wiped users configurations"
            return 0
        else
            echo_error "Unable to clean the directory"
            return 1
        fi

    else
        echo_error "Problem found while trying to wipe the printer config directory, zip file might not have been created... Exiting installation"
        return 1
    fi
}

function install_config() {
    echo_info "[INSTALL] Installing new RD50 BLOCKS printer configuration."
    ## Check if a config folder exists
    if [ -d "${USER_CONFIG_PATH}" ]; then
        echo_ok "[INSTALL] User Klipper config path exists. Continuing..."
    fi

    # Iterate over all files in the update, and create symlinks for them
    echo_ok "[INSTALL] adding new configuration files to Klippers printer_data directory"

    for dir in *; do
        if [ "$(basename "${dir}")" == "printer.cfg" ]; then 

            continue 
        fi 
        if [[ "$(create_symlink "${RD50_Klipper_Configs_PATH}/${dir}" "${USER_CONFIG_PATH}/${dir}")" -eq 1 ]]; then
            echo_ok "\t\t [INSTALL] Symlink of ${RD50_Klipper_Configs_PATH}/${dir} created on ${USER_CONFIG_PATH}/${dir}"
        else
            echo_error "[INSTALL] ERROR: Unable to create symlink of ${RD50_Klipper_Configs_PATH}/${dir} on ${USER_CONFIG_PATH}/${dir}. Exiting installation!!!"
            return 1
        fi
    done

    #*# Copy the printer.cfg file to the directory| -f if an existing cannot be opened delete and try again | -n preserve attributes
    cp -fp "${RD50_Klipper_Configs_PATH}/printer.cfg" "${USER_CONFIG_PATH}" 
    return 0
}

function create_symlink() {
    local target_file=$1
    local link_name=$2
    # Create and check id the symlink was successfully created
    if [[ "$(sudo ln -fs "${target_file}" "${link_name}")" -eq 0 ]]; then
        # echo_ok "Symlink ${target_file} with ${link_name}."
        return 0
    else
        # echo_error "Problem creating symlink of ${target_file} on ${link_name}. Exiting."
        return 1
    fi
}

function restart_klipper() {
    echo_info "Restarting Klipper"
    sudo systemctl restart klipper
}

function restart_moonraker() {
    echo_info "Restarting Moonraker"
    sudo systemctl restart moonraker
}

function install() {

    printf "\n================================================\n"
    echo_info "Initializing Blocks Configurations installation."
    printf "\n================================================\n"

    echo_info "Starting backup procedure of current configuration files\n\n"
    backup
    if [ $? -gt 1 ]; then # Check if the last command was executed with an exit status.
        echo_error "[BACKUP] Unsuccessful, exiting Blocks configuration installation."
        exit 1
    fi

    echo_info "Installing new configuration files to the user printer directory"
    install_config

    restart_klipper
    restart_moonraker
    # if [ -z "$START" ] || [ "$START" -eq 0 ]; then
    #     echo_ok "Blocks installation ended successfully."
    # else
    #     echo_error "Fuck"
    # fi
}

install
exit 0
