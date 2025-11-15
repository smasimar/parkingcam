import os
import sys
import cv2
import time
import logging
import signal
import configparser
from datetime import datetime
from contextlib import contextmanager
from PIL import Image, ImageDraw, ImageFont

# RPi DHT sensor and SPI display related imports
import board
import adafruit_dht
sys.path.append("..")
from lib import LCD_1inch69


####################################################################################################
# FFmpeg error suppression
####################################################################################################

@contextmanager
def suppress_stderr():
    """Context manager to suppress stderr (for FFmpeg H.264 decoding errors)"""
    with open(os.devnull, 'w') as devnull:
        old_stderr = sys.stderr
        sys.stderr = devnull
        try:
            yield
        finally:
            sys.stderr = old_stderr

# Set FFmpeg log level to suppress verbose H.264 decoding errors
# These are non-fatal errors that occur with packet loss/corruption
os.environ['OPENCV_FFMPEG_LOGLEVEL'] = '-8'  # Quiet mode (only fatal errors)
os.environ['OPENCV_LOG_LEVEL'] = 'ERROR'  # Reduce OpenCV verbosity

####################################################################################################
# Configuration management
####################################################################################################

def load_config(config_path='parkingcam.conf', logger=None):
    """Load configuration from .conf file"""
    config = configparser.ConfigParser()
    
    if not os.path.exists(config_path):
        if logger:
            logger.warning(f"Config file {config_path} not found. Using defaults.")
        return get_default_config()
    
    config.read(config_path)
    
    # Normalize boolean values - ensure they're lowercase strings
    # ConfigParser.getboolean() is case-sensitive for some values
    for section in ['CLOCK', 'TEMPERATURE', 'HUMIDITY']:
        if config.has_section(section) and config.has_option(section, 'enabled'):
            value = config.get(section, 'enabled').strip().lower()
            config.set(section, 'enabled', value)
    # Normalize ROI and DETECTION boolean values
    if config.has_section('ROI') and config.has_option('ROI', 'use_full_frame'):
        value = config.get('ROI', 'use_full_frame').strip().lower()
        config.set('ROI', 'use_full_frame', value)
    if config.has_section('DETECTION') and config.has_option('DETECTION', 'show_statusbar'):
        value = config.get('DETECTION', 'show_statusbar').strip().lower()
        config.set('DETECTION', 'show_statusbar', value)
    
    return config

def get_font_path(config):
    """Get font path from config with fallback"""
    try:
        if config.has_section('DISPLAY') and config.has_option('DISPLAY', 'font_path'):
            font_path = config.get('DISPLAY', 'font_path').strip()
            if font_path:
                return font_path
    except Exception:
        pass
    # Default fallback
    return "assets/RobotoMonoMedium.ttf"

def load_font(font_path, size, logger=None):
    """Load font with fallback to default if file not found"""
    try:
        return ImageFont.truetype(font_path, size)
    except Exception as e:
        if logger:
            logger.warning(f"Could not load font '{font_path}': {e}. Using default font.")
        return ImageFont.load_default()

def get_config_bool(config, section, option, fallback=False):
    """Safely get boolean value from config, handling various formats"""
    try:
        if config.has_section(section) and config.has_option(section, option):
            value = config.get(section, option).strip().lower()
            # Handle various boolean representations
            if value in ('true', '1', 'yes', 'on'):
                return True
            elif value in ('false', '0', 'no', 'off', ''):
                return False
            else:
                # Try ConfigParser's built-in method
                return config.getboolean(section, option, fallback=fallback)
        return fallback
    except (ValueError, configparser.NoOptionError, configparser.NoSectionError):
        return fallback

def get_default_config():
    """Return default configuration"""
    config = configparser.ConfigParser()
    config['RTSP'] = {'username': '', 'password': '', 'address': '', 'timeout': '10'}
    config['VIDEO'] = {'use_local_file': 'false', 'local_file_path': ''}
    config['ROI'] = {'x': '800', 'y': '500', 'width': '550', 'height': '580', 'use_full_frame': 'false'}
    config['TEMPERATURE'] = {'enabled': 'false', 'min_value': '16', 'ideal_value': '22', 'max_value': '28'}
    config['HUMIDITY'] = {'enabled': 'false', 'min_value': '25', 'ideal_value': '50', 'max_value': '75'}
    config['DETECTION'] = {'confidence_threshold': '0.4', 'history_size': '120', 
                           'car_present_threshold': '80', 'car_absent_threshold': '40', 
                           'show_statusbar': 'true', 'cv_interval': '1.0'}
    config['CLOCK'] = {'enabled': 'false'}
    config['DISPLAY'] = {'font_path': 'assets/RobotoMonoMedium.ttf'}
    return config

def get_rtsp_url(config):
    """Construct RTSP URL from config, return None if not configured
    Supports both authenticated (username:password@address) and anonymous (address) streams
    Adds TCP transport parameter for better reliability (reduces decoding errors)
    """
    username = config.get('RTSP', 'username', fallback='').strip()
    password = config.get('RTSP', 'password', fallback='').strip()
    address = config.get('RTSP', 'address', fallback='').strip()
    
    # Address is required
    if not address:
        return None
    
    # Build base URL
    if username and password:
        base_url = f"rtsp://{username}:{password}@{address}"
    elif not username and not password:
        base_url = f"rtsp://{address}"
    else:
        # Invalid: only one credential provided
        return None
    
    # Add TCP transport parameter for better reliability (reduces decoding errors)
    # Use ?transport=tcp to force TCP instead of UDP
    if '?' in base_url:
        url = f"{base_url}&transport=tcp"
    else:
        url = f"{base_url}?transport=tcp"
    
    return url

####################################################################################################
# RPi DHT sensor and SPI display related function definitions
####################################################################################################

def display_init():
    """Initialize the SPI display"""
    try:
        log.debug('Start display initialization')
        disp = LCD_1inch69.LCD_1inch69()
        # Initialize library
        disp.Init()
        # Clear display
        disp.clear()
        # Set the backlight
        disp.bl_DutyCycle(100)
        log.debug('Finish display initialization')
        return disp
    except IOError as e:
        log.error(f"Display initialization failed: {e}")
        return None
    except Exception as e:
        log.error(f"Unexpected error during display initialization: {e}")
        return None

def display_draw_status(disp, car_history, car_image, config, sensor=None):
    """Draw status on the display"""
    if disp is None:
        return
    
    try:
        font_path = get_font_path(config)
        font = load_font(font_path, 64, log)
        font_sm = load_font(font_path, 32, log)
        canvas = Image.new("RGB", (disp.width, disp.height), (0, 0, 0))
        width, height = car_image.size
        
        # Check if we should fit full frame or use ROI
        use_full_frame = get_config_bool(config, 'ROI', 'use_full_frame', fallback=False)
        statusbar_height = 40 if get_config_bool(config, 'DETECTION', 'show_statusbar', fallback=True) else 0
        
        # Clock, temp, and humidity are always enabled (if sensor available for temp/humidity)
        temp_enabled = sensor is not None
        humi_enabled = sensor is not None
        
        # Left panel for temp, humidity (stacked vertically, overlayed on video)
        left_panel_width = disp.width // 2
        
        # Calculate panel heights based on font size + margins
        # Create a temporary draw object to measure font metrics
        temp_draw = ImageDraw.Draw(canvas)
        
        # Clock panel: font size + margin top + margin bottom (always enabled)
        # Note: margin value must match the margin used in draw_clock_panel()
        clock_margin = 8
        clock_bbox = temp_draw.textbbox((0, 0), "00:00", font=font)
        clock_font_height = clock_bbox[3] - clock_bbox[1]
        clock_panel_height = clock_font_height + (clock_margin * 2)
        
        # Temp/Humidity panels: font size + margin top + margin bottom
        # Note: margin value must match the margin used in draw_temp_panel() and draw_humi_panel()
        sensor_margin = 4
        if temp_enabled or humi_enabled:
            # Measure font height using sample text
            sensor_bbox = temp_draw.textbbox((0, 0), "00.0ºC", font=font_sm)
            sensor_font_height = sensor_bbox[3] - sensor_bbox[1]
            sensor_panel_height = sensor_font_height + (sensor_margin * 2)
        else:
            sensor_panel_height = 0
        
        temp_panel_height = sensor_panel_height if temp_enabled else 0
        humi_panel_height = sensor_panel_height if humi_enabled else 0
        
        # Calculate video area: starts below clock, uses remaining space
        video_start_y = statusbar_height + clock_panel_height
        available_height = disp.height - video_start_y  # Remaining height below clock
        available_width = disp.width
        
        if use_full_frame:
            # Fit to height: scale video to match available height, crop sides if needed
            scale = available_height / height
            new_height = available_height
            new_width = int(width * scale)
            
            # Resize maintaining aspect ratio
            car_image = car_image.resize((new_width, new_height), Image.Resampling.LANCZOS)
            
            # If scaled width is wider than screen, crop sides (center crop horizontally)
            if new_width > available_width:
                crop_left = (new_width - available_width) // 2
                car_image = car_image.crop((crop_left, 0, crop_left + available_width, new_height))
                x_offset = 0
            else:
                # If scaled width is narrower, center it (shouldn't happen often)
                x_offset = (available_width - new_width) // 2
            
            # Position below clock
            y_offset = video_start_y
        else:
            # ROI mode: fit to height and crop sides
            # Validate ROI bounds
            roi_x_clamped = max(0, min(roi_x, width - 1))
            roi_y_clamped = max(0, min(roi_y, height - 1))
            roi_w_clamped = min(roi_w, width - roi_x_clamped)
            roi_h_clamped = min(roi_h, height - roi_y_clamped)
            
            # Extract ROI
            car_image = car_image.crop((roi_x_clamped, roi_y_clamped, 
                                       roi_x_clamped + roi_w_clamped, 
                                       roi_y_clamped + roi_h_clamped))
            roi_width, roi_height = car_image.size
            
            # Fit to height: scale ROI to match available height, crop sides if needed
            scale = available_height / roi_height
            new_height = available_height
            new_width = int(roi_width * scale)
            
            # Resize maintaining aspect ratio
            car_image = car_image.resize((new_width, new_height), Image.Resampling.LANCZOS)
            
            # If scaled width is wider than screen, crop sides (center crop horizontally)
            if new_width > available_width:
                crop_left = (new_width - available_width) // 2
                car_image = car_image.crop((crop_left, 0, crop_left + available_width, new_height))
                x_offset = 0
            else:
                # If scaled width is narrower, center it
                x_offset = (available_width - new_width) // 2
            
            # Position below clock
            y_offset = video_start_y

        # Paste the processed image (full screen)
        canvas.paste(car_image, (x_offset, y_offset))

        draw = ImageDraw.Draw(canvas)
        # Draw status bar if enabled
        if get_config_bool(config, 'DETECTION', 'show_statusbar', fallback=True):
            for i, car_present in enumerate(car_history):
                if car_present:
                    draw.rectangle([(i*2, 0), ((i+1)*2, 40)], fill="GREEN")
                else:
                    draw.rectangle([(i*2, 0), ((i+1)*2, 40)], fill="RED")

        # Draw clock row (always enabled, full width)
        clock_y = statusbar_height
        draw_clock_panel(canvas, font, x=0, y=clock_y, width=disp.width, height=clock_panel_height, bg_color=(127, 0, 127))
        
        # Draw temperature and humidity panels (left half, stacked vertically, overlayed on video)
        panel_x = 0
        panel_y = video_start_y  # Start at video area (below clock)
        
        # Temperature panel
        if temp_enabled:
            draw_temp_panel(canvas, font_sm, x=panel_x, y=panel_y, width=left_panel_width, height=temp_panel_height, 
                          sensor=sensor, config=config)
            panel_y += temp_panel_height
        
        # Humidity panel
        if humi_enabled:
            draw_humi_panel(canvas, font_sm, x=panel_x, y=panel_y, width=left_panel_width, height=humi_panel_height, 
                          sensor=sensor, config=config)

        disp.ShowImage(canvas)
    except Exception as e:
        log.error(f"Error drawing display status: {e}")

def draw_clock_panel(img, font, x, y, width, height, bg_color=(127, 0, 127), text_color='white'):
    """Draw clock as a left-side panel with centered text"""
    draw = ImageDraw.Draw(img)
    
    # Get current time
    now = datetime.now()
    hours = now.strftime('%H')
    minutes = now.strftime('%M')
    
    # Calculate text dimensions
    hours_bbox = draw.textbbox((0, 0), hours, font=font)
    hours_width = hours_bbox[2] - hours_bbox[0]
    hours_height = hours_bbox[3] - hours_bbox[1]
    
    minutes_bbox = draw.textbbox((0, 0), minutes, font=font)
    minutes_width = minutes_bbox[2] - minutes_bbox[0]
    
    colon_bbox = draw.textbbox((0, 0), ':', font=font)
    colon_width = colon_bbox[2] - colon_bbox[0]
    
    # Calculate total width and starting position for centering text within panel
    total_width = hours_width + colon_width + minutes_width
    hours_left = hours_bbox[0]  # Offset from origin
    hours_top = hours_bbox[1]   # Offset from origin
    start_x = x + (width - total_width) // 2 - hours_left
    
    # Center text vertically within panel with 5px margins top and bottom
    margin = 8
    available_height = height - (margin * 2)  # Subtract top and bottom margins
    text_y = y + margin + (available_height - hours_height) // 2 - hours_top
    
    # Determine if colon should be visible (blink every second)
    colon_visible = int(time.time()) % 2 == 0
    
    # Draw panel background
    draw.rectangle([x, y, x + width, y + height], fill=bg_color)
    
    # Draw hours
    draw.text((start_x, text_y), hours, font=font, fill=text_color)
    
    # Draw colon (blinking)
    colon_x = start_x + hours_width
    if colon_visible:
        draw.text((colon_x, text_y), ':', font=font, fill=text_color)
    else:
        # Draw colon in background color to make it "invisible" but maintain spacing
        draw.text((colon_x, text_y), ':', font=font, fill=bg_color)
    
    # Draw minutes
    minutes_x = start_x + hours_width + colon_width
    draw.text((minutes_x, text_y), minutes, font=font, fill=text_color)

    return img

def draw_temp_panel(img, font, x, y, width, height, sensor, config):
    """Draw temperature as a left-side panel with centered text"""
    draw = ImageDraw.Draw(img)
    
    temp_text = None
    temp_color = None
    
    # Get temperature
    try:
        temp = sensor.temperature
        if temp is not None:
            min_temp = config.getfloat('TEMPERATURE', 'min_value', fallback=16)
            ideal_temp = config.getfloat('TEMPERATURE', 'ideal_value', fallback=22)
            max_temp = config.getfloat('TEMPERATURE', 'max_value', fallback=28)
            temp_color = interpolate_color(temp, min_temp, ideal_temp, max_temp)
            temp_text = f"{temp:0.1f}ºC"
    except RuntimeError:
        log.warning('DHT temperature reading failed')
    except Exception as e:
        log.debug(f"Temperature display error: {e}")
    
    if not temp_text:
        return img
    
    # Calculate text dimensions
    temp_bbox = draw.textbbox((0, 0), temp_text, font=font)
    temp_width = temp_bbox[2] - temp_bbox[0]
    temp_height = temp_bbox[3] - temp_bbox[1]
    temp_left = temp_bbox[0]
    temp_top = temp_bbox[1]
    
    # Center text horizontally and vertically within panel with margins
    margin = 4  # Top and bottom margin
    temp_x = x + (width - temp_width) // 2 - temp_left
    available_height = height - (margin * 2)  # Subtract top and bottom margins
    temp_y = y + margin + (available_height - temp_height) // 2 - temp_top
    
    # Draw panel background
    draw.rectangle([x, y, x + width, y + height], fill=temp_color if temp_color else (127, 127, 127))
    
    # Draw text
    draw.text((temp_x, temp_y), temp_text, font=font, fill='black')
    
    return img

def draw_humi_panel(img, font, x, y, width, height, sensor, config):
    """Draw humidity as a left-side panel with centered text"""
    draw = ImageDraw.Draw(img)
    
    humi_text = None
    humi_color = None
    
    # Get humidity
    try:
        humi = sensor.humidity
        if humi is not None:
            min_humi = config.getfloat('HUMIDITY', 'min_value', fallback=25)
            ideal_humi = config.getfloat('HUMIDITY', 'ideal_value', fallback=50)
            max_humi = config.getfloat('HUMIDITY', 'max_value', fallback=75)
            humi_color = interpolate_color(humi, min_humi, ideal_humi, max_humi)
            humi_text = f"{humi:0.1f}🌢%"
    except RuntimeError:
        log.warning('DHT humidity reading failed')
    except Exception as e:
        log.debug(f"Humidity display error: {e}")
    
    if not humi_text:
        return img
    
    # Calculate text dimensions
    humi_bbox = draw.textbbox((0, 0), humi_text, font=font)
    humi_width = humi_bbox[2] - humi_bbox[0]
    humi_height = humi_bbox[3] - humi_bbox[1]
    humi_left = humi_bbox[0]
    humi_top = humi_bbox[1]
    
    # Center text horizontally and vertically within panel with margins
    margin = 8  # Top and bottom margin
    humi_x = x + (width - humi_width) // 2 - humi_left
    available_height = height - (margin * 2)  # Subtract top and bottom margins
    humi_y = y + margin + (available_height - humi_height) // 2 - humi_top
    
    # Draw panel background
    draw.rectangle([x, y, x + width, y + height], fill=humi_color if humi_color else (127, 127, 127))
    
    # Draw text
    draw.text((humi_x, humi_y), humi_text, font=font, fill='black')
    
    return img


def interpolate_color(curr_value, min_value, ideal_value, max_value):
    """Interpolate color based on value (red -> yellow -> green)"""
    red = (255, 0, 0)
    yellow = (255, 255, 0)
    green = (0, 255, 0)

    if curr_value < ideal_value:
        if curr_value <= min_value:
            return red
        ratio = (curr_value - min_value) / (ideal_value - min_value)
        return (
            int(red[0] + ratio * (green[0] - red[0])),
            int(red[1] + ratio * (green[1] - red[1])),
            int(red[2] + ratio * (green[2] - red[2]))
        )
    else:
        if curr_value >= max_value:
            return red
        ratio = (curr_value - ideal_value) / (max_value - ideal_value)
        return (
            int(green[0] + ratio * (red[0] - green[0])),
            int(green[1] + ratio * (red[1] - green[1])),
            int(green[2] + ratio * (red[2] - green[2]))
        )

def display_exit(disp):
    """Clean up display resources"""
    if disp is not None:
        try:
            disp.module_exit()
        except Exception as e:
            log.error(f"Error during display exit: {e}")

####################################################################################################
# Common functionality
####################################################################################################

# Global flag for graceful shutdown
shutdown_flag = False

def signal_handler(signum, frame):
    """Handle shutdown signals"""
    global shutdown_flag
    log.info("Shutdown signal received. Cleaning up...")
    shutdown_flag = True


def draw_statusbar(car_history, debug_image, config, display, sensor):
    """Draw status bar on display and log status"""
    if display is not None:
        display_draw_status(display, car_history, debug_image, config, sensor)

    statusbar = ''
    for i, car_present in enumerate(car_history):
        statusbar = f'{statusbar}✔️ ' if car_present else f'{statusbar}❌ '
    log.debug(statusbar)

def overlay_bounding_boxes(image_pil, detections, roi_width, roi_height, config=None):
    """Overlay bounding boxes on an existing PIL image (YOLO detections)"""
    if detections is None:
        return image_pil
    
    font_size = 24
    font_path = get_font_path(config) if config else "assets/RobotoMonoMedium.ttf"
    font = load_font(font_path, font_size, log)

    outlinecolor = (0, 0, 0)
    draw = ImageDraw.Draw(image_pil)

    # YOLO detections (COCO classes)
    # COCO class IDs: 0:person, 2:car, 8:boat, 39:bottle, 56:chair, 62:tv
    coco_class_names = {0: 'person', 2: 'car', 8: 'boat', 39: 'bottle', 56: 'chair', 62: 'tv'}
    coco_class_colors = {0: (255, 255, 0), 2: (0, 0, 255), 8: (255, 0, 0), 
                        39: (0, 255, 0), 56: (255, 0, 255), 62: (0, 255, 255)}
    
    # Process YOLO results
    if detections is not None and len(detections) > 0:
        for result in detections:
            boxes = result.boxes
            if len(boxes) > 0:
                for i in range(len(boxes)):
                    confidence = float(boxes.conf[i].item() if hasattr(boxes.conf[i], 'item') else boxes.conf[i])
                    class_id = int(boxes.cls[i].item() if hasattr(boxes.cls[i], 'item') else boxes.cls[i])
                    
                    # Get bounding box coordinates
                    xyxy = boxes.xyxy[i].cpu().numpy() if hasattr(boxes.xyxy[i], 'cpu') else boxes.xyxy[i].numpy()
                    x1, y1, x2, y2 = xyxy
                    startX, startY, endX, endY = int(x1), int(y1), int(x2), int(y2)
                    
                    class_name = coco_class_names.get(class_id, f"Class {class_id}")
                    label = f"{class_name}: {confidence:.2f}"
                    class_color = coco_class_colors.get(class_id, (127, 127, 127))
                    
                    # Draw bounding box
                    draw.rectangle([(startX, startY), (endX, endY)], outline=class_color, width=2)
                    
                    # Draw text with outline
                    Δ = 2
                    captionX = startX + Δ
                    captionY = endY - font_size - Δ
                    # Draw the text outline
                    draw.text((captionX - Δ, captionY - Δ), label, font=font, fill=outlinecolor)
                    draw.text((captionX + Δ, captionY - Δ), label, font=font, fill=outlinecolor)
                    draw.text((captionX - Δ, captionY + Δ), label, font=font, fill=outlinecolor)
                    draw.text((captionX + Δ, captionY + Δ), label, font=font, fill=outlinecolor)
                    # Draw the text over it
                    draw.text((captionX, captionY), label, font=font, fill=class_color)

    return image_pil

def connect_to_local_file(file_path):
    """Open a local video file for playback
    
    Args:
        file_path: Path to the video file (MP4, AVI, etc.)
    """
    if file_path is None or not file_path.strip():
        return None
    
    file_path = file_path.strip()
    
    # Check if file exists
    if not os.path.exists(file_path):
        log.error(f"Video file not found: {file_path}")
        return None
    
    try:
        # Suppress FFmpeg stderr during file opening
        with suppress_stderr():
            cap = cv2.VideoCapture(file_path)
            
            if not cap.isOpened():
                log.warning(f"Failed to open video file: {file_path}")
                return None
            
            # Try to read a test frame to verify the file works
            ret, test_frame = cap.read()
            if not ret or test_frame is None:
                log.warning(f"Video file opened but failed to read test frame: {file_path}")
                cap.release()
                return None
            
            # Reset to beginning for actual playback
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        
        log.info(f"Successfully opened video file: {file_path} (resolution: {int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))})")
        return cap
    except Exception as e:
        log.error(f"Error opening video file: {e}")
        return None

def connect_to_rtsp_stream(rtsp_url, timeout_seconds=10):
    """Connect/reconnect to the RTSP stream with optimized settings for reliability
    
    Args:
        rtsp_url: RTSP stream URL
        timeout_seconds: Connection timeout in seconds (default: 10)
    """
    if rtsp_url is None:
        return None
    
    try:
        # Suppress FFmpeg stderr during connection to avoid H.264 decoding error spam
        with suppress_stderr():
            # Use CAP_FFMPEG backend explicitly for better RTSP support
            # Set timeout before opening (in milliseconds)
            timeout_ms = timeout_seconds * 1000
            
            # Try to set FFmpeg options via environment variable for timeout
            # This helps prevent the 30-second default timeout
            original_timeout = os.environ.get('OPENCV_FFMPEG_READ_TIMEOUT_MSEC')
            os.environ['OPENCV_FFMPEG_READ_TIMEOUT_MSEC'] = str(timeout_ms)
            
            try:
                cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
                
                # Set additional properties for better RTSP handling
                # Set connection timeout (if supported)
                cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, timeout_ms)
                cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, timeout_ms)
                
                if not cap.isOpened():
                    log.warning(f"Failed to connect to RTSP stream (timeout: {timeout_seconds}s)")
                    if cap is not None:
                        cap.release()
                    return None
                
                # Set buffer size to reduce latency and improve stability
                # Smaller buffer = lower latency but may drop frames
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                
                # Try to read a test frame to verify the connection works
                # Use a shorter timeout for the test read
                cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, timeout_ms)
                ret, test_frame = cap.read()
                if not ret or test_frame is None:
                    log.warning(f"RTSP stream connected but failed to read test frame (timeout: {timeout_seconds}s)")
                    cap.release()
                    return None
            finally:
                # Restore original timeout setting if it existed
                if original_timeout is not None:
                    os.environ['OPENCV_FFMPEG_READ_TIMEOUT_MSEC'] = original_timeout
                elif 'OPENCV_FFMPEG_READ_TIMEOUT_MSEC' in os.environ:
                    del os.environ['OPENCV_FFMPEG_READ_TIMEOUT_MSEC']
        
        log.info(f"Successfully connected to RTSP stream (resolution: {int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))}x{int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))})")
        return cap
    except Exception as e:
        log.error(f"Error connecting to RTSP stream: {e}")
        return None

def create_placeholder_image(width, height, config=None):
    """Create a placeholder image when RTSP is not available"""
    img = Image.new('RGB', (width, height), color=(64, 64, 64))
    draw = ImageDraw.Draw(img)
    font_path = get_font_path(config) if config else "assets/RobotoMonoMedium.ttf"
    font = load_font(font_path, 32, log)
    
    text = "No RTSP Stream"
    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    position = ((width - text_width) // 2, (height - text_height) // 2 + 100)
    draw.text(position, text, font=font, fill=(255, 255, 255))
    return img

####################################################################################################
# Main execution
####################################################################################################

# Setup logging (default to INFO level)
logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s: %(message)s'
)
log = logging.getLogger(__name__)

# Load configuration
config = load_config('parkingcam.conf', log)

# Setup signal handlers for graceful shutdown
signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

# Initialize display
display = display_init()
if display is None:
    log.warning("Display initialization failed. Continuing without display.")

# Initialize DHT22 sensor (always enabled if available)
sensor = None
try:
    sensor = adafruit_dht.DHT22(board.D4)
    log.info("DHT22 sensor initialized")
except Exception as e:
    log.warning(f"Failed to initialize DHT22 sensor: {e}. Continuing without sensor.")

# Load YOLOv11 model
yolo_model = None
yolo_version = None

# Load YOLOv11 (best accuracy and efficiency)
try:
    from ultralytics import YOLO
    # YOLOv11 will auto-download on first use if not present
    # Using YOLOv11n (nano) for RPi - lowest CPU usage while maintaining good accuracy
    yolo_model = YOLO('yolo11n.pt')  # Will download if not found
    yolo_version = "YOLOv11"
    log.info("YOLOv11 model loaded successfully")
except ImportError:
    log.error("ultralytics package not available. Please install it: pip install ultralytics")
    log.error("Exiting.")
    sys.exit(1)
except Exception as e:
    log.error(f"Failed to load YOLOv11 model: {e}")
    log.error("Exiting.")
    sys.exit(1)

# Check if using local file or RTSP stream
use_local_file = get_config_bool(config, 'VIDEO', 'use_local_file', fallback=False)
local_file_path = config.get('VIDEO', 'local_file_path', fallback='').strip()

cap = None
rtsp_url = None
rtsp_timeout = 10

if use_local_file:
    # Use local video file
    if not local_file_path:
        log.warning("Local file mode enabled but no file path specified. App will run without video stream.")
    else:
        log.info(f"Using local video file: {local_file_path}")
        cap = connect_to_local_file(local_file_path)
else:
    # Use RTSP stream
    rtsp_url = get_rtsp_url(config)
    if rtsp_url is None:
        log.warning("RTSP stream not configured. App will run without video stream.")
    else:
        log.info("RTSP stream configured")
        rtsp_timeout = config.getint('RTSP', 'timeout', fallback=10)
        cap = connect_to_rtsp_stream(rtsp_url, timeout_seconds=rtsp_timeout)

# Get configuration values
roi_x = config.getint('ROI', 'x', fallback=800)
roi_y = config.getint('ROI', 'y', fallback=500)
roi_w = config.getint('ROI', 'width', fallback=550)
roi_h = config.getint('ROI', 'height', fallback=580)
use_full_frame = get_config_bool(config, 'ROI', 'use_full_frame', fallback=False)

cv_interval = config.getfloat('DETECTION', 'cv_interval', fallback=1.0)
confidence_threshold = config.getfloat('DETECTION', 'confidence_threshold', fallback=0.4)
history_size = config.getint('DETECTION', 'history_size', fallback=120)
car_present_threshold = config.getint('DETECTION', 'car_present_threshold', fallback=80)
car_absent_threshold = config.getint('DETECTION', 'car_absent_threshold', fallback=40)

# Parking spot status
last_cv_time = time.time()
car_history = []

# Shared state for display and CV results (updated by CV thread, read by display loop)
latest_detections = None
latest_detection_frame_size = None  # (width, height) for coordinate scaling
cv_processing = False  # Flag to prevent multiple CV threads
display_lock = None
try:
    import threading
    display_lock = threading.Lock()
    HAS_THREADING = True
except ImportError:
    HAS_THREADING = False


def process_cv_detection(frame, use_full_frame, roi_x, roi_y, roi_w, roi_h, 
                         yolo_model, confidence_threshold):
    """Process a frame with YOLO detection (runs in separate thread or at intervals)
    Returns: (car_detected, detections, frame_size) where detections can be used for overlay
    """
    try:
        car_detected = False
        detections = None
        
        # Determine what region to use for detection
        if use_full_frame:
            # Use entire frame for detection
            detection_frame = frame.copy()  # Copy to avoid issues if frame is modified
            frame_h, frame_w = frame.shape[:2]
            frame_size = (frame_w, frame_h)
        else:
            # Extract ROI from frame (with bounds validation)
            frame_h, frame_w = frame.shape[:2]
            # Validate ROI bounds
            if roi_x < 0 or roi_y < 0 or roi_x + roi_w > frame_w or roi_y + roi_h > frame_h:
                log.warning(f"ROI bounds out of range: frame={frame_w}x{frame_h}, ROI=({roi_x},{roi_y},{roi_w},{roi_h}). Clamping ROI.")
                roi_x = max(0, min(roi_x, frame_w - 1))
                roi_y = max(0, min(roi_y, frame_h - 1))
                roi_w = min(roi_w, frame_w - roi_x)
                roi_h = min(roi_h, frame_h - roi_y)
            
            detection_frame = frame[roi_y:roi_y+roi_h, roi_x:roi_x+roi_w].copy()
            frame_h, frame_w = roi_h, roi_w
            frame_size = (frame_w, frame_h)
        
        # YOLO detection (COCO classes)
        # COCO class IDs: 0:person, 2:car, 8:boat, 39:bottle, 56:chair, 62:tv
        if yolo_model is not None:
            results = yolo_model(detection_frame, conf=confidence_threshold, verbose=False)
            detections = results
            
            # Check for relevant classes (lighting/reflection workaround)
            for result in results:
                boxes = result.boxes
                if len(boxes) > 0:
                    for i in range(len(boxes)):
                        class_id = int(boxes.cls[i].item() if hasattr(boxes.cls[i], 'item') else boxes.cls[i])
                        confidence = float(boxes.conf[i].item() if hasattr(boxes.conf[i], 'item') else boxes.conf[i])
                        # COCO classes: 0:person, 2:car, 8:boat, 39:bottle, 56:chair, 62:tv
                        if class_id in [0, 2, 8, 39, 56, 62]:
                            car_detected = True
                            break
                    if car_detected:
                        break
        
        return car_detected, detections, frame_size
    except Exception as e:
        log.error(f"Error processing frame with CV: {e}")
        if use_full_frame:
            frame_h, frame_w = frame.shape[:2] if frame is not None else (480, 640)
        else:
            frame_w, frame_h = roi_w, roi_h
        return False, None, (frame_w, frame_h)

def prepare_display_image(frame, use_full_frame, roi_x, roi_y, roi_w, roi_h, config=None):
    """Prepare frame for display (without CV processing)"""
    if frame is None:
        if use_full_frame:
            return create_placeholder_image(640, 480, config)
        else:
            return create_placeholder_image(roi_w, roi_h, config)
    
    try:
        if use_full_frame:
            # Convert full frame to PIL Image
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            return Image.fromarray(frame_rgb)
        else:
            # Extract ROI and convert to PIL Image (with bounds validation)
            frame_h, frame_w = frame.shape[:2]
            # Validate ROI bounds
            if roi_x < 0 or roi_y < 0 or roi_x + roi_w > frame_w or roi_y + roi_h > frame_h:
                log.warning(f"ROI bounds out of range in display: frame={frame_w}x{frame_h}, ROI=({roi_x},{roi_y},{roi_w},{roi_h}). Clamping ROI.")
                roi_x = max(0, min(roi_x, frame_w - 1))
                roi_y = max(0, min(roi_y, frame_h - 1))
                roi_w = min(roi_w, frame_w - roi_x)
                roi_h = min(roi_h, frame_h - roi_y)
            
            roi = frame[roi_y:roi_y+roi_h, roi_x:roi_x+roi_w]
            roi_rgb = cv2.cvtColor(roi, cv2.COLOR_BGR2RGB)
            return Image.fromarray(roi_rgb)
    except Exception as e:
        log.error(f"Error preparing display image: {e}")
        if use_full_frame:
            return create_placeholder_image(640, 480, config)
        else:
            return create_placeholder_image(roi_w, roi_h, config)

def cv_processing_thread(frame, use_full_frame, roi_x, roi_y, roi_w, roi_h,
                        yolo_model, confidence_threshold,
                        car_history, history_size, car_present_threshold, car_absent_threshold):
    """Background thread function for CV processing"""
    global latest_detections, latest_detection_frame_size, cv_processing
    
    try:
        car_detected, detections, frame_size = process_cv_detection(
            frame, use_full_frame, roi_x, roi_y, roi_w, roi_h,
            yolo_model, confidence_threshold
        )
        
        # Update shared detection results
        if display_lock:
            with display_lock:
                latest_detections = detections
                latest_detection_frame_size = frame_size
        else:
            latest_detections = detections
            latest_detection_frame_size = frame_size
        
        # Update car history (thread-safe)
        if display_lock:
            with display_lock:
                car_history.append(car_detected)
                if len(car_history) > history_size:
                    car_history.pop(0)
                
        else:
            car_history.append(car_detected)
            if len(car_history) > history_size:
                car_history.pop(0)
    except Exception as e:
        log.error(f"Error in CV processing thread: {e}")
    finally:
        cv_processing = False

log.info("Starting parking spot monitoring...")

try:
    while not shutdown_flag:
        # Handle video source (RTSP stream or local file)
        if use_local_file:
            # Local file mode
            if cap is None:  # If file is not opened, try to open it
                log.debug("Attempting to open local video file...")
                cap = connect_to_local_file(local_file_path)
                time.sleep(1)
                continue
            
            # Read frame from local file
            try:
                with suppress_stderr():
                    ret, frame = cap.read()
            except Exception as e:
                log.error(f"Error reading frame from file: {e}")
                ret = False
                frame = None
            
            # If end of file reached, restart from beginning (loop)
            if not ret:
                log.debug("End of video file reached. Restarting from beginning...")
                if cap is not None:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    ret, frame = cap.read()
                    if not ret:
                        log.warning("Failed to restart video file. Reopening...")
                        cap.release()
                        cap = connect_to_local_file(local_file_path)
                        time.sleep(0.5)
                        continue
        elif rtsp_url is not None:
            # RTSP stream mode
            if cap is None:  # If the stream is not connected, try to reconnect
                log.debug("Attempting to reconnect to RTSP stream...")
                cap = connect_to_rtsp_stream(rtsp_url, timeout_seconds=rtsp_timeout)
                time.sleep(1)
                continue

            # Suppress FFmpeg stderr during frame reading to avoid H.264 decoding error spam
            # These errors are non-fatal and occur with packet loss/corruption
            try:
                with suppress_stderr():
                    ret, frame = cap.read()
            except Exception as e:
                log.error(f"Error reading frame (decoding error?): {e}")
                ret = False
                frame = None
            
            # If frame is not grabbed, try a few more times before reconnecting
            if not ret:
                # Try reading a few more frames in case of temporary glitch
                retry_count = 0
                while retry_count < 3 and not ret:
                    time.sleep(0.1)
                    try:
                        with suppress_stderr():
                            ret, frame = cap.read()
                    except Exception as e:
                        log.debug(f"Retry read failed: {e}")
                        ret = False
                        frame = None
                    retry_count += 1
                
                if not ret:
                    log.warning("Failed to grab frame after retries. Reconnecting to the stream...")
                    if cap is not None:
                        cap.release()
                    cap = connect_to_rtsp_stream(rtsp_url, timeout_seconds=rtsp_timeout)
                    time.sleep(5)
                    continue
        else:
            # No video source - create placeholder
            frame = None
            ret = False

        # Get the current time
        current_time = time.time()

        # Check if CV should run (at configured interval)
        cv_should_run = (current_time - last_cv_time >= cv_interval) and not cv_processing
        
        # Start CV processing thread if it's time and we have a frame
        if cv_should_run and frame is not None and ret and HAS_THREADING:
            last_cv_time = current_time
            cv_processing = True
            # Start CV processing in background thread (non-blocking)
            thread = threading.Thread(
                target=cv_processing_thread,
                args=(frame.copy(), use_full_frame, roi_x, roi_y, roi_w, roi_h,
                      yolo_model, confidence_threshold,
                      car_history, history_size, car_present_threshold, car_absent_threshold),
                daemon=True
            )
            thread.start()
        elif cv_should_run and frame is not None and ret and not HAS_THREADING:
            # Fallback: run CV synchronously if threading not available (not ideal)
            last_cv_time = current_time
            car_detected, detections, frame_size = process_cv_detection(
                frame, use_full_frame, roi_x, roi_y, roi_w, roi_h,
                yolo_model, confidence_threshold
            )
            latest_detections = detections
            latest_detection_frame_size = frame_size
            # Update car history (thread-safe)
            if display_lock:
                with display_lock:
                    car_history.append(car_detected)
                    if len(car_history) > history_size:
                        car_history.pop(0)
            else:
                car_history.append(car_detected)
                if len(car_history) > history_size:
                    car_history.pop(0)
        elif cv_should_run:
            # No frame for CV - append False to history (thread-safe)
            last_cv_time = current_time
            if display_lock:
                with display_lock:
                    car_history.append(False)
                    if len(car_history) > history_size:
                        car_history.pop(0)
            else:
                car_history.append(False)
                if len(car_history) > history_size:
                    car_history.pop(0)
        
        # Always prepare and display the current frame (continuous video)
        if frame is not None and ret:
            display_image = prepare_display_image(frame, use_full_frame, roi_x, roi_y, roi_w, roi_h, config=config)
        else:
            # No frame available - use placeholder
            if use_full_frame:
                display_image = create_placeholder_image(640, 480, config=config)
            else:
                display_image = create_placeholder_image(roi_w, roi_h, config=config)
        
        # Overlay bounding boxes from latest CV results if available
        if display_lock:
            with display_lock:
                current_detections = latest_detections
                current_detection_frame_size = latest_detection_frame_size
        else:
            current_detections = latest_detections
            current_detection_frame_size = latest_detection_frame_size
        
        # Overlay bounding boxes if we have detections and frame sizes match
        if current_detections is not None and current_detection_frame_size is not None:
            # Get display image dimensions
            display_w, display_h = display_image.size
            det_w, det_h = current_detection_frame_size
            
            # Only overlay if dimensions match (same region)
            if display_w == det_w and display_h == det_h:
                display_image = overlay_bounding_boxes(
                    display_image, current_detections, display_w, display_h, config=config
                )
        
        # Display the image with statusbar (thread-safe read of car_history)
        if display_lock:
            with display_lock:
                current_car_history = car_history.copy()  # Copy for thread safety
        else:
            current_car_history = car_history
        draw_statusbar(current_car_history, display_image, config, display, sensor)

        # Small sleep to prevent tight loop (allows ~30fps display)
        time.sleep(0.033)  # ~30fps

except KeyboardInterrupt:
    log.info("Interrupted by user")
except Exception as e:
    log.error(f"Unexpected error: {e}", exc_info=True)
finally:
    # Cleanup
    log.info("Cleaning up resources...")
    if cap is not None:
        cap.release()
    display_exit(display)
    log.info("Shutdown complete")
