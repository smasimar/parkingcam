import os
import cv2
import time
import argparse
import subprocess
import numpy as np
from datetime import datetime
from dotenv import load_dotenv
from pystray import Icon, MenuItem, Menu
from PIL import Image, ImageDraw, ImageFont

# Function to create and manage the system tray icon
def create_icon():
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
    if not args.notray:
        icon.run_detached()

    return icon

# Function to open VLC with a specific XSPF file
def open_vlc():
    vlc_path = "C:\\Program Files\\VLC\\vlc.exe"  # Update with your VLC path
    subprocess.Popen([vlc_path, rtsp_url])

# Function to change the taskbar icon based on parking status
def update_icon_state(icon, state):
    if state == "taken":
        icon.icon = Image.open("assets/car_red.ico")
    else:
        icon.icon = Image.open("assets/car_green.ico")

# Function to log car activity (arrival and departure)
def log_car_activity(action, timestamp):
    log_entry=f'{timestamp} :: {action}'
    debug_log(log_entry)
    with open('car_log.txt', 'a') as log_file:
        log_file.write(f'{log_entry}\n')

# Function to output debug logs
def debug_log(message):
    if args.debug:
        print(message)

# Function to save an image with bounding boxes and class_id in debug mode
def save_debug_image(roi, detections):
    class_names = {0:'background', 1:'aeroplane', 2: 'bicycle', 3: 'bird', 4: 'boat',
                    5: 'bottle', 6: 'bus', 7: 'car', 8: 'cat', 9: 'chair', 10: 'cow', 
                    11: 'diningtable', 12: 'dog', 13: 'horse', 14: 'motorbike', 15: 'person', 
                    16: 'pottedplant', 17: 'sheep', 18: 'sofa', 19: 'train', 20: 'tvmonitor'}
    class_colors = {7: (255, 0, 255), 4: (255, 0, 255), 9: (255, 0, 255), 20: (255, 0, 255)}
    outlinecolor = (0, 0, 0) # Black outline

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
            roi = np.array(image_pil) 
        
    # Save the debug image
    debug_image_path = f"debug/debug_output_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.jpg"
    cv2.imwrite(debug_image_path, roi)
    debug_log(f"Debug image saved: {debug_image_path}")

# Function to connect/reconnect to the RTSP stream
def connect_to_rtsp_stream(rtsp_url):
    cap = cv2.VideoCapture(rtsp_url)
    if not cap.isOpened():
        debug_log(f"Failed to connect to the RTSP stream at {rtsp_url}")
        return None
    else:
        debug_log(f"Successfully connected to the RTSP stream at {rtsp_url}")
    return cap

# Argument parser to handle the --debug flag
parser = argparse.ArgumentParser(description="Car detection script.")
parser.add_argument("--debug", action="store_true", help="Enable debug mode for detailed logging.")
parser.add_argument("--image", action="store_true", help="Enable debug image output.")
parser.add_argument("--notray", action="store_true", help="Disable tray icon.")
args = parser.parse_args()

# Create system tray icon
icon = create_icon()

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

font_path = "assets/RobotoMonoMedium.ttf"
font_size = 24
font = ImageFont.truetype(font_path, font_size)

# Interval to run the recognition (in seconds)
recognition_interval = 30

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
        debug_log("Failed to grab frame. Reconnecting to the stream...")
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

        car_detected = False

        # Process detections
        for i in range(detections.shape[2]):
            confidence = detections[0, 0, i, 2]
            if confidence > 0.4:  # Confidence threshold for detection
                class_id = int(detections[0, 0, i, 1])
                if class_id in [4, 7, 9, 20]: # anything goes, depending on lighting and reflections
                    car_detected = True
                    break

        # Append result to detection history for averaging
        car_history.append(car_detected)

        # Keep only the last 10 frames in history
        if len(car_history) > 10:
            car_history.pop(0)

        # Decide if the car is present based on the majority of recent detections
        if sum(car_history) > 5:  # More than 5 out of the last 10 frames detect a car
            car_present = True
        else:
            car_present = False

        timestamp_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # Check if the car was present and is now gone (car just left)
        if car_present and not car_detected and not car_left_time:
            log_car_activity("Car left the parking spot", timestamp_str)
            car_present = False
            car_left_time = timestamp_str
            update_icon_state(icon, "free") # Update taskbar icon to green (free)

        # Check if the car just arrived back in the spot
        if not car_present and car_detected and car_left_time:
            log_car_activity("Car arrived back", timestamp_str)
            car_present = True
            car_left_time = None
            update_icon_state(icon, "taken") # Update taskbar icon to red (taken)

        history=''
        for i, car in enumerate(car_history):
            record = '☑' if car else '☒'
            history = f'{history}{record} '

        # If in debug mode, log information and save the image with visual output
        if args.debug:
            debug_log(history)
            debug_log(f"Current status: car_present = {car_present}, car_detected = {car_detected}")

        if args.image:
            save_debug_image(roi, detections)

    # Sleep for a short time (optional) to reduce CPU load
    time.sleep(1)

cap.release()