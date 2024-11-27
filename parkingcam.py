import os
import sys
import cv2
import time
import random
import logging
import platform
import argparse
import subprocess
import numpy as np
from datetime import datetime, time as dtime
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

# Checking if running on Windows or RPi
is_windows = platform.system() == "Windows"

####################################################################################################
# Windows system tray-related function definitions
####################################################################################################

# Only load pystray on Windows to prevent RPi issues
if is_windows:
    from pystray import Icon, MenuItem, Menu

# Function to create and manage the system tray icon
def icon_init():
    # Do not initialize if not needed
    if args.notray:
        return None

    # Create a pystray Icon instance
    icon = Icon("Parking Spot Monitor")
    icon.title = "Parking Spot Monitor"

    # Set the initial icon
    icon.icon = Image.open("assets/car_red.ico")

    # Add menu item to quit and open VLC
    icon.menu = Menu(
        MenuItem('Open VLC', open_vlc),
        MenuItem('Quit', lambda: icon.stop())
    )

    # Run the icon
    icon.run_detached()

    return icon

# Function to open VLC with a specific XSPF file
def open_vlc():
    vlc_path = "C:\\Program Files\\VLC\\vlc.exe"  # Update with your VLC path
    subprocess.Popen([vlc_path, rtsp_url])

# Function to change the taskbar icon based on parking status
def update_icon_state(icon, state):
    if icon is None:
        return

    if state == "taken":
        icon.icon = Image.open("assets/car_red.ico")
    else:
        icon.icon = Image.open("assets/car_green.ico")

####################################################################################################
# RPi DHT sensor and SPI display related function definitions
####################################################################################################

if not is_windows:
    import board
    import adafruit_dht
    import spidev as SPI
    sys.path.append("..")
    from lib import LCD_1inch69

def display_init():
    try:
        log.debug('Start display initialization')
        # Display with hardware SPI:
        # Warning!!!Don't create multiple display objects!!!
        # disp = LCD_1inch69.LCD_1inch69(spi=SPI.SpiDev(bus, device),spi_freq=10000000,rst=RST,dc=DC,bl=BL)
        RST = 27
        DC = 25
        BL = 18
        bus = 0 
        device = 0
        disp = LCD_1inch69.LCD_1inch69()
        # Initialize library
        disp.Init()
        # Clear display
        disp.clear()
        # Set the backlight
        disp.bl_DutyCycle(100)
        log.debug('Finish display initialization')

    except IOError as e:
        log.error(e)    

    return disp

def display_draw_status(disp, car_history, car_image):
    font = ImageFont.truetype("assets/RobotoMonoMedium.ttf", 64)
    font_sm = ImageFont.truetype("assets/RobotoMonoMedium.ttf", 32)
    canvas = Image.new("RGB", (disp.width,disp.height), (255, 0, 255))
    width, height = car_image.size
    size=(240, 240)
    
    # Determine if the image is portrait or landscape
    if height > width:  # Portrait
        # Crop a square from the bottom
        new_height = width
        left = 0
        top = height - width  # Bottom crop
        right = width
        bottom = height
    else:  # Landscape or square
        # Crop a square from the center
        new_width = height
        left = (width - height) // 2  # Center crop
        top = 0
        right = left + height
        bottom = height

    # Crop the image
    car_image = car_image.crop((left, top, right, bottom))
    # Resize the cropped image to 240x240
    car_image = car_image.resize(size)

    # Statusbar size = 240x40
    # Statusbar entry size = 24x40
    canvas.paste(car_image, (0, 40))

    draw = ImageDraw.Draw(canvas)
    for i, car_present in enumerate(car_history):
        if car_present:
            draw.rectangle([(i*2, 0), ((i+1)*2, 40)], fill = "GREEN")
        else:
            draw.rectangle([(i*2, 0), ((i+1)*2, 40)], fill = "RED")

    if args.clock:
        if int(time.time()) % 2:
            statustext_time = datetime.now().strftime('%H:%M')
        else:
            statustext_time = datetime.now().strftime('%H %M')
        draw_text_with_background(
            img=canvas,
            text=statustext_time,
            font=font,
            position=(120, 45),  # Position of the text (center of the text will be at (250, 250))
            alignment='center',  # Alignment options: 'center', 'left', 'right'
            text_color='white',  # Color of the text
            bg_color=(127, 0, 127)  # Background color 
        )

    if args.sensor:
        try:
            temp = sensor.temperature
            humi = sensor.humidity
        except RuntimeError:
            log.warning('DHT reading failed')
        else: 
            temp_color = interpolate_color(temp,16,22,28)
            draw_text_with_background(
                img=canvas,
                text=f"{temp:0.1f}ºC",
                font=font_sm,
                position=(5, 120),
                alignment='left',
                text_color='black',
                bg_color=temp_color
            )

            humi_color = interpolate_color(humi,25,50,75)
            draw_text_with_background(
                img=canvas,
                text=f"{humi:0.1f}%",
                font=font_sm,
                position=(240, 120),
                alignment='right',
                text_color='black',
                bg_color=humi_color
            )

    disp.ShowImage(canvas)

def draw_text_with_background(img, text, font, position, alignment='center', text_color='black', bg_color='yellow'):
    draw = ImageDraw.Draw(img)

    # Get the size of the text (bounding box)
    text_bbox = draw.textbbox((0, 0), text, font=font)
    text_width = text_bbox[2] - text_bbox[0]
    text_height = text_bbox[3] - text_bbox[1]

    # Calculate the position based on the alignment
    x, y = position

    if alignment == 'center':
        x -= text_width // 2
    elif alignment == 'right':
        x -= text_width
    # If alignment is 'left', we don't need to adjust x

    # Draw a rectangle behind the text (background)
    padding = 10  # Padding around the text
    draw.rectangle(
        [x - padding, y, x + text_width + padding, y + text_height + padding*2],
        fill=bg_color
    )

    # Draw the text on top of the rectangle
    draw.text((x, y), text, font=font, fill=text_color)

    return img

def interpolate_color(curr_value, min_value, ideal_value, max_value):
    # Define RGB values for red, yellow, and green
    red = (255, 0, 0)
    yellow = (255, 255, 0)
    green = (0, 255, 0)

    # Normalize value to a range between -1 and 1, where 0 is ideal value
    if curr_value < ideal_value:
        # Min side: from red (min_value) to green (ideal_value)
        if curr_value <= min_value:
            return red
        ratio = (curr_value - min_value) / (ideal_value - min_value)
        return (
            int(red[0] + ratio * (green[0] - red[0])),  # Interpolate R
            int(red[1] + ratio * (green[1] - red[1])),  # Interpolate G
            int(red[2] + ratio * (green[2] - red[2]))   # Interpolate B
        )
    else:
        # Max side: from green (ideal_value) to red (max_value)
        if curr_value >= max_value:
            return red
        ratio = (curr_value - ideal_value) / (max_value - ideal_value)
        return (
            int(green[0] + ratio * (red[0] - green[0])),  # Interpolate R
            int(green[1] + ratio * (red[1] - green[1])),  # Interpolate G
            int(green[2] + ratio * (red[2] - green[2]))   # Interpolate B
        )

def display_exit(disp):
    disp.module_exit()

####################################################################################################
# Common functionality
####################################################################################################

# Function to log car activity (arrival and departure)
def log_car_activity(timestamp, action):
    log_entry=f'{timestamp} :: {action}'
    log.info(log_entry)
    with open('car.log', 'a') as log_file:
        log_file.write(f'{log_entry}\n')

def draw_statusbar(car_history, debug_image):
    
    if not is_windows:
        display_draw_status(display, car_history, debug_image)

    statusbar = ''
    for i, car_present in enumerate(car_history):
        statusbar = f'{statusbar}✔️ ' if car_present else f'{statusbar}❌ '
    log.debug(statusbar)

    return None

# Function to save an image with bounding boxes and class_id in debug mode
def draw_debug_image(roi, detections):

    # Initiate font for debug image output
    font_size = 24
    font = ImageFont.truetype("assets/RobotoMonoMedium.ttf", font_size)

    class_names = {0:'background', 1:'aeroplane', 2: 'bicycle', 3: 'bird', 4: 'boat',
                    5: 'bottle', 6: 'bus', 7: 'car', 8: 'cat', 9: 'chair', 10: 'cow', 
                    11: 'diningtable', 12: 'dog', 13: 'horse', 14: 'motorbike', 15: 'person', 
                    16: 'pottedplant', 17: 'sheep', 18: 'sofa', 19: 'train', 20: 'tvmonitor'}
    class_colors = {4: (0, 255, 0), 7: (255, 0, 0), 9: (255, 0, 255), 15: (255, 255, 0), 20: (0, 255, 255)}
    outlinecolor = (0, 0, 0) # Black outline
    image_pil = Image.fromarray(roi)

    # Loop over all detections and draw the bounding boxes
    for i in range(detections.shape[2]):
        confidence = detections[0, 0, i, 2]
        if confidence > 0.4:  # Confidence threshold for detection
            class_id = int(detections[0, 0, i, 1])

            class_name = class_names.get(class_id, f"Class {class_id}")
            label = f"{class_name}: {confidence:.2f}"

            class_color = class_colors.get(class_id,(127, 127, 127)) # Gray for undefined

            # Draw bounding box
            box = detections[0, 0, i, 3:7] * np.array([w, h, w, h])
            (startX, startY, endX, endY) = box.astype("int")
            cv2.rectangle(roi, (startX, startY), (endX, endY), class_color, 2)

            # Convert OpenCV image to PIL image
            image_pil = Image.fromarray(roi)
            draw = ImageDraw.Draw(image_pil)

            Δ = 2
            startY = startY-font_size-Δ
            # Draw the text outline
            draw.text((startX-Δ, startY-Δ), label, font=font, fill=outlinecolor)
            draw.text((startX+Δ, startY-Δ), label, font=font, fill=outlinecolor)
            draw.text((startX-Δ, startY+Δ), label, font=font, fill=outlinecolor)
            draw.text((startX+Δ, startY+Δ), label, font=font, fill=outlinecolor)
            # Draw the text over it
            draw.text((startX, startY), label, font=font, fill=class_color)
            
            # Convert PIL image back to OpenCV image
            # roi = np.array(image_pil)  

    return image_pil

# Function to connect/reconnect to the RTSP stream
def connect_to_rtsp_stream(rtsp_url):
    cap = cv2.VideoCapture(rtsp_url)
    if not cap.isOpened():
        log.error(f"Failed to connect to {rtsp_url}")
        return None
    else:
        log.info(f"Successfully connected to {rtsp_url}")
    return cap

####################################################################################################
####################################################################################################
####################################################################################################

# Argument parser to handle the --debug flag
parser = argparse.ArgumentParser(description="Car detection script.")
parser.add_argument("--debug", action="store_true", help="Enable debug logging.")
parser.add_argument("--image", action="store_true", help="Enable debug image output.")
parser.add_argument("--notray", action="store_true", help="Disable Windows tray icon.")
parser.add_argument("--clock",action="store_true", help="Display clock on the SPI Display")
parser.add_argument("--sensor",action="store_true", help="Display DHT22 readings on the SPI Display")

args = parser.parse_args()

if args.debug:
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(levelname)s: %(message)s'
    )
else:
    logging.basicConfig(
        level=logging.INFO,
        format='%(levelname)s: %(message)s'
    )
log = logging.getLogger(__name__)

# Initialize system tray icon
if is_windows:
    icon = icon_init()
else:
    display = display_init()
    sensor = adafruit_dht.DHT22(board.D4)

# Load pre-trained object detection model (https://github.com/chuanqi305/MobileNet-SSD)
net = cv2.dnn.readNetFromCaffe('assets/deploy.prototxt', 'assets/mobilenet_iter_73000.caffemodel')

# Load environment variables from .env file
load_dotenv()

# Access sensitive information from environment variables
rtsp_username = os.getenv('RTSP_USERNAME')
rtsp_password = os.getenv('RTSP_PASSWORD')
rtsp_address = os.getenv('RTSP_ADDRESS')

# Construct the RTSP URL
rtsp_url = f"rtsp://{rtsp_username}:{rtsp_password}@{rtsp_address}"

# Initial connection to the RTSP stream
cap = connect_to_rtsp_stream(rtsp_url)

# Interval to run the recognition (in seconds)
recognition_interval = 1

# Parking spot status: False means no car, True means car present
car_present = False
# Time tracking for when the car left
car_left_time = None
# Track the last time recognition was run
last_recognition_time = time.time()
# Add a buffer to store detection results
car_history = []

while True:
    if cap is None:  # If the stream is not connected, try to reconnect
        cap = connect_to_rtsp_stream(rtsp_url)
        time.sleep(1)
        continue

    ret, frame = cap.read()
    
    # If frame is not grabbed, reconnect to the stream
    if not ret:
        log.warning("Failed to grab frame. Reconnecting to the stream...")
        cap.release()  # Release the previous connection
        cap = connect_to_rtsp_stream(rtsp_url)  # Reconnect to the stream
        time.sleep(5)  # Add a small delay to avoid tight looping
        continue  # Skip this iteration and try again

    # Get the current time
    current_time = time.time()

    # Only run recognition once every minute (or based on the interval)
    if current_time - last_recognition_time >= recognition_interval:
        # Update the last recognition time
        last_recognition_time = current_time

        # Define the region of interest (ROI) for the parking spot
        x, y, w, h = 800, 500, 550, 580  # Adjust these coordinates for your setup
        roi = frame[y:y+h, x:x+w]

        # Prepare the frame for object detection
        blob = cv2.dnn.blobFromImage(roi, 0.007843, (300, 300), 127.5)
        net.setInput(blob)
        detections = net.forward()

        debug_image = draw_debug_image(roi, detections)

        car_detected = False

        # Process detections
        for i in range(detections.shape[2]):
            confidence = detections[0, 0, i, 2]
            if confidence > 0.4:  # Confidence threshold for detection
                class_id = int(detections[0, 0, i, 1])
                if class_id in [4, 7, 9, 15, 20]: # anything goes, depending on lighting and reflections
                    car_detected = True
                    break

        timestamp_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # Append result to detection history for averaging
        car_history.append(car_detected)

        # Keep only the last 10 frames in history
        if len(car_history) > 120:
            car_history.pop(0)

        # Get the current time
        current_time = datetime.now().time()
        # Define the start and end times
        start_time = dtime(8, 0)  # 08:00
        end_time = dtime(20, 0)   # 20:00

        if start_time <= current_time <= end_time:
            # Decide if the car is present based on the majority of recent detections
            if sum(car_history) >= 80:  # More than 80 out of the last 120 frames detect a car
                if not car_present:
                    log_car_activity(timestamp_str, "Car arrived back")
                    if is_windows:
                        update_icon_state(icon, "taken") # Update taskbar icon to red (taken)
                car_present = True
            elif sum(car_history) <= 40: # Less than 40 out of the last 120 frames detect no car
                if car_present:
                    log_car_activity(timestamp_str, "Car left the parking spot")
                    if is_windows:
                        update_icon_state(icon, "free") # Update taskbar icon to green (free)
                car_present = False
        
        draw_statusbar(car_history, debug_image)

        # Save the debug image
        if args.image:
            debug_image_path = f"debug/debug_output_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.jpg"
            # if cv2.imwrite(debug_image_path, roi):
            try:
                debug_image.save(debug_image_path)
            except IOError:
                log.warning(f"Couldn't save debug image: {debug_image_path}")   
            else:
                log.debug(f"Debug image saved: {debug_image_path}")


    # Sleep for a short time (optional) to reduce CPU load
    time.sleep(0.1)

cap.release()