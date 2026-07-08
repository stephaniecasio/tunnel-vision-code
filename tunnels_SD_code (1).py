# collecting data from sensors and cameras and sending to SD card via .csv and .h264
# dealing with DHT22 (T/RH), Raspberry Pi 3B+, 5137 Digikey light (switched w transistor), 8MP*4 IMX219 Arducam (4 cams, 1 active)
# Lily Secord, Tunnel Vision, SEED 2026


# ****************** ELECTRICAL NOTES ********************
# arducam's select lines use pins 7, 11, 12, which are BCM GPIO4, GPIO17, GPIO18
# DHT22 data --> GPIO23 (physical pin 16), 10k pull-up to 3.3V
# ring light gate/base --> GPIO24 (physical pin 18), through a resistor
# ********************************************************

# notes: before running, enable legacy cam and I2C (sudo raspi-config), can't wire ring
# light directly to GPIO24 -- need transistor, need libraries

# ************* BEFORE RUNNING ****************
    # sudo raspi-config --> interface options --> legacy camera --> enable
        # board is switched with raspistill/raspivide, needs legacy stacks
        # sudo raspi-config --> interface options --> I2C --> enable, sudo reboot
    # pip3 install adafruit-circuitpython-dht RPi.GPIO, sudo apt-get install libgpiod2
    # wiring:
        # DHT22: VCC --> 3.3V, GND --> GND, DATA --> GPIO23 (BCM) with 10k ohm pull-up resistor btwn DATA and VCC
        # cam adapter select pins (BCM numbering): GPIO4, GPIO17, GPIO18 (physical pins 7, 11, 12)
        # ring light: don't wire straight to GPIO pin. use NPN/MOSFET transistor: 
            # ring light (+) --> 5V, ring light (-) --> transistor drain/collector, transistor source/emitter --> GND,
            # transistor gate/base --> GPIO24 (BCM) through a resistor. GPIO24 switches the transistor on/off
# *********************************************



# if want to turn light off when enough light present, need photoresistor or something




# imports
import csv
import os
import subprocess
import threading
import time
from datetime import datetime

import RPi.GPIO as GPIO     # controls GPIO pins on pi 
    # note: if running directly on pi, open terminal and install: sudo apt update (new line) sudo apt install python3-rpi.gpio
import adafruit_dht
    # must install library in terminal: pip install adafruit-circuitpython-blinka (new line) pip install adafruit-circuitpython-dht
import board    # allows adafruit library to talk to physical pins on pi

 

# ****** CONFIGURATION ******
# temporary for testing
OUTPUT_DIR = "rover_data_test_stage1"
#OUTPUT_DIR = "/home/pi/rover_data" # points to folder on pi mapped to SD card
VIDEO_DIR = os.path.join(OUTPUT_DIR, "video")   # saves video files to folder ^^^
CSV_PATH = os.path.join(OUTPUT_DIR, "log.csv")  # logs data on csv and maps to main folder

DHT_GPIO = board.D23     # BCM GPIO23 --> DHT connection point on pi
RING_LIGHT_PIN = 24     # BCM GPIO24 --> transistor gate/base

# board select GPIO triples for each camera channel on the arducam multi-camera adapter V2.2
    # BCM numbering: GPIO4, GPIO17, GPIO18 (physical pins 7, 11, 12)
CAM_SELECT_PINS = (4, 17, 18)   # which GPIO pins on pi are wired to arducam's switching chips
# pi has one camera channel --> arducam has four cameras
# to capture an image from specific camera, will cross reference with the table and flip pins on/off depending on the combination
    # arducam detects combo and routes camera's video feed to pi
CAMERA_CHANNELS = {             # maps each camera channel (4 chips) to specific combo of elec signals across the three pins
    1: (False, False, True),
    2: (True, False, True),
    3: (False, True, False),
    4: (True, True, False),
}       # true = high/on, false = low/off


# add more channel nums here once adtl cameras wired
ACTIVE_CAMERAS = [1]    # list of camera channels want rover to use

VIDEO_WIDTH = 1280              # 1280 pixels wide (std res for video)
VIDEO_HEIGHT = 720              # 720 pixels high (std res for video)
VIDEO_FPS = 30                  # frames per second
VIDEO_SEGMENT_SECONDS = 60      # length of each video file before beginning next

DHT_POLL_INTERVAL_SECONDS = 2.0     # lowest possible reliably

# shared state so sensor-logging thread knows which video file is currently being recorded
# so T/RH rows can be matched to vid segments
_state_lock = threading.Lock()      
_shared_state = {"video_file": None, "camera": None}    # running at same time but not interfering w each other

# ****** SETUP ******
# creates output and video folders on SD card and starts csv with header row if one doesn't already exist 
def setup_dirs():
    os.makedirs(VIDEO_DIR, exist_ok = True)     # creates video folder; if folder alr exists doesn't crash
    if not os.path.exists(CSV_PATH):            # checks if data file already exists at CSV_PATH -- if already there, function 
        with open(CSV_PATH, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                ["timestamp",       # time
                 "temperature_F",   # temp
                 "humidity_pct",    # rel humidity
                 "active_camera",   # which camera
                 "video_file",      # video
                ]
            )

# prepares GPIO pins for use 
def setup_gpio():
    GPIO.setwarnings(False)    # disables GPIO warning messages (makes output cleaner)
    GPIO.setmode(GPIO.BCM)     # sets GPIO to BCM (pin numbers refer to GPIO rather than physical numbers)

    for pin in CAM_SELECT_PINS:            # loops through every GPIO pin stored in list
        GPIO.setup(pin, GPIO.OUT)          # configures camera pin as output channel

    GPIO.setup(RING_LIGHT_PIN, GPIO.OUT)   # configures GPIO pin connected to ring light as output channel
    GPIO.output(RING_LIGHT_PIN, GPIO.LOW)   # turns light off to begin

# ****** RING LIGHT ******
# drives the transistor gate/base high or low to switch light on or off,
# recording current state so logger thread can write it to csv
def ring_light(on: bool):
    GPIO.output(RING_LIGHT_PIN, GPIO.HIGH if on else GPIO.LOW)

# ****** CAMERA ******
# drives arducam adapter's 3 select lines to route power and data to one of 4 camera channels
def select_camera(channel: int):
    p4, p17, p18 = CAMERA_CHANNELS[channel]     
    GPIO.output(CAM_SELECT_PINS[0], p4)        
    GPIO.output(CAM_SELECT_PINS[1], p17)
    GPIO.output(CAM_SELECT_PINS[2], p18)
    time.sleep(0.3)  # allow sensor to settle before capturing

# selects given camera channel and records one fixed length video segment to SD card using raspivid
def record_video_segment(channel: int, timestamp: str) -> str:
    select_camera(channel)      # calls the function select_camera(), switching to specific camera
    filename = f"cam{channel}_{timestamp}.h264"     # creates filename using f-string
    filepath = os.path.join(VIDEO_DIR, filename)    # combines video directory with filename
    subprocess.run(
        [
            "raspivid",         # captures video from camera
            "-o", filepath,     # specifies output file
            "-t", str(VIDEO_SEGMENT_SECONDS * 1000),    # specifies recording duration (ms)
            "-w", str(VIDEO_WIDTH),     # sets video width
            "-h", str(VIDEO_HEIGHT),    # sets video height
            "-fps", str(VIDEO_FPS),     # sets frame rate
            "-n",   # no on-screen preview, headless
        ],
        check=True,     # verifies that raspivid successfully ran (if True, program continues)
    )
    return filepath     # returns full path to saved video

# ******* DHT22 **********
def read_dht22_farenheit(sensor, retries=5):
    for _ in range(retries):       # creates loop running up to specified number
        try:
            temperature_c = sensor.temperature      # reads temp value from sensor, returns in C
            humidity = sensor.humidity              # reads RH value from sensor, returns in RH%
            if temperature_c is not None and humidity is not None:      # checks both readings are valid
                temperature_f = (temperature_c * (9.0 / 5.0)) + 32.0    # converts temp from C to F
                return round(temperature_f, 1), round(humidity, 1)      # rounds RH and temp to 1 decimal place, returns them as tuple
        except RuntimeError:        # catches RuntimeError (DHT22 read glitches are common, retries)
            pass    # if error, does nothing (tries again)
        time.sleep(1)       # waits a second before trying to read again (sensor stabilizes)
    return None, None       # if retries all fail, returns this indicating no valid readings could be taken

# runs cont. in background for as long as deviced powered on. independent of video segment timing,
# logs whichever video file is currently being recorded so T/RH rows can be later matched to video
def sensor_logging_loop(sensor, stop_event: threading.Event):
    with open(CSV_PATH, "a", newline="") as f:      # opens csv file in append mode ("a") -> if file already exists, data appended
        writer = csv.writer(f)  # creates csv writer object used to write rows into file
        while not stop_event.is_set():  # starts loop that continues running until thread signals it to stop
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")    # grabs current date and time, formats as string, stored in csv
            temp_f, humidity = read_dht22_farenheit(sensor)     # calls sensor reading function

            with _state_lock:   # thread lock (only one thread accesses shared data at a time, prevents errors)
                video_file = _shared_state["video_file"]    # reads info
                camera = _shared_state["camera"]            # reads info

            writer.writerow([timestamp, temp_f, humidity, camera, video_file])  # writes one row into csv file
            f.flush()       # forces system to write buffered data to disk

            print(f"[{timestamp}] T = {temp_f}F | RH = {humidity}% | cam{camera} -> {video_file}")      # prints status message to terminal

            stop_event.wait(DHT_POLL_INTERVAL_SECONDS)      # pauses thread for specified polling interval (waits before taking next measurement)

# ******* MAIN **********
def main():
    setup_dirs()    # creates directory and csv file
    #setup_gpio()    # initializes pi GPIO pins

    # TEMPORARY REPLACEMENT TO TEST PI -- leave only setup_dirs()
    print("GPIO initialized.")  # verifies GPIO library works, no pin numbering errors, no wiring issues
    time.sleep(5)
    GPIO.cleanup()

    # testing order: 
    # directory creation
    # GPIO initialization
    # ring light
    # DHT22 sensor
    # single camera recording
    # camera switching
    # CSV logging thread
    # entire program

    # TO TEST ONLY DHT22 -- leave only setup_dirs()
    #sensor = adafruit_dht.DHT22(DHT_GPIO, use_pulseio=False)
    #try:
        #while True:
            #temp, rh = read_dht22_farenheit(sensor)
            #print(temp, rh)
            #time.sleep(2)

    #except KeyboardInterrupt:
        #sensor.exit()

    # TO TEST ONLY RING LIGHT --  verifies transistor and GPIO24 wired correctly
    # leave only setup_gpio()
    #print("light on")
    #ring_light(True)
    #time.sleep(5)
    #print("light off")
    #ring_light(False)
    #GPIO.cleanup()

    # TO TEST ONLY CAMERA -- leave both setup_dirs() and setup_gpio()
    #timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    #record_video_segment(1, timestamp)
    #GPIO.cleanup()

    # TEST CAM SWITCHING -- leave both setup functions
    #for cam in ACTIVE_CAMERAS:
        #timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        #print(f"Recording camera {cam}")
        #record_video_segment(cam, timestamp)
    #GPIO.cleanup()

    # TEST LOGGING THREAD -- keep only setup_dirs()
    #sensor = adafruit_dht.DHT22(DHT_GPIO, use_pulseio=False)
    #_shared_state["camera"] = 1
    #_shared_state["video_file"] = "test.h264"
    #stop_event = threading.Event()
    #logger = threading.Thread(
        #target = sensor_logging_loop,
        #args=(sensor, stop_event),
    #)
    #logger.start()
    #time.sleep(20)
    #stop_event.set()
    #logger.join()
    #sensor.exit()

    # open log.csv and confirm rows were added

    # UNCOMMENT FOR REAL (APART OF MAIN)
#     sensor = adafruit_dht.DHT22(DHT_GPIO, use_pulseio = False)      # creates DHT22 sensor object

#     stop_event = threading.Event()      # creates event that stops logging thread later
    
#     logger_thread = threading.Thread(       # creates new thread: reads DHT22 sensor, logs to csv
#         target = sensor_logging_loop, args = (sensor, stop_event), daemon=True
#     )

#     try:
#         ring_light(True)    # turns light on, stays on for entire run
#         logger_thread.start()   # starts background logging thread
        
#         cam_index = 0   # initializes camera index
#         while True:     # runs until interrupted
#             channel = ACTIVE_CAMERAS[cam_index % len(ACTIVE_CAMERAS)]   # selects next camera in list
#             timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")        # creates timestamp that becomes part of video filename

#             # allows camera loop to safely run and update considering simultaneous with DHT22 data
#             with _state_lock:   # locks shared info
#                 _shared_state["camera"] = channel       
#                 _shared_state["video_file"] = f"cam{channel}_{timestamp}.h264"

#             record_video_segment(channel, timestamp)    # records video
#             cam_index += 1      # moves to next camera and reruns loop

#     except KeyboardInterrupt:       # if user presses ctrl + c, KeyboardInterrupt
#         print("Stopped by user.")
#     finally:        # executes no matter what
#         stop_event.set()    # signals logger thread to stop
#         logger_thread.join(timeout=5)   # waits up to 5 seconds for logging thread to cleanly finish
#         ring_light(False)   # turns light off
#         sensor.exit()       # releases DHT22 sensor resources
#         GPIO.cleanup()      # resets GPIO pins, leaving pi in clean state for future


# if __name__ == "__main__":      # if file runs directly then main is executed (if file imported into another python program, main() not automatically called)
#     main()    
