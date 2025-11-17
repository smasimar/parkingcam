#!/usr/bin/env python3
import os
import sys
import cv2
import time
import logging
import signal
import configparser
import argparse
import hashlib
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
    config['RTSP'] = {'url': '', 'timeout': '10'}
    config['VIDEO'] = {'use_local_file': 'false', 'local_file_path': ''}
    config['ROI'] = {'roi_method': 'point_quadrant', 'point_x': '450', 'point_y': '400', 'quadrant': '4',
                     'x': '800', 'y': '500', 'width': '550', 'height': '580', 'use_full_frame': 'false'}
    config['TEMPERATURE'] = {'enabled': 'false', 'min_value': '16', 'ideal_value': '22', 'max_value': '28'}
    config['HUMIDITY'] = {'enabled': 'false', 'min_value': '25', 'ideal_value': '50', 'max_value': '75'}
    config['DETECTION'] = {'confidence_threshold': '0.4', 'history_size': '120', 
                           'car_present_threshold': '80', 'car_absent_threshold': '40', 
                           'show_statusbar': 'true', 'cv_interval': '1.0',
                           'temporal_smoothing_cycles': '1'}
    config['CLOCK'] = {'enabled': 'false'}
    config['DISPLAY'] = {'font_path': 'assets/RobotoMonoMedium.ttf'}
    return config

def get_rtsp_url(config):
    """Get RTSP URL from config, return None if not configured
    
    Reads the full RTSP URL from config (format: rtsp://username:password@address:port/path)
    Adds TCP transport parameter for better reliability (reduces decoding errors) if not present.
    
    Args:
        config: Configuration object
    
    Returns:
        Complete RTSP URL with TCP transport parameter, or None if not configured
    """
    rtsp_url = config.get('RTSP', 'url', fallback='').strip()
    
    # URL is required
    if not rtsp_url:
        return None
    
    # Add rtsp:// prefix if not present (allows config to omit prefix)
    if not rtsp_url.startswith('rtsp://'):
        rtsp_url = f"rtsp://{rtsp_url}"
    
    # Add TCP transport parameter for better reliability (reduces packet loss)
    # Use ?transport=tcp to force TCP instead of UDP
    if 'transport=' in rtsp_url:
        # Transport parameter already present, return as-is
        return rtsp_url
    elif '?' in rtsp_url:
        # URL already has query parameters, append with &
        return f"{rtsp_url}&transport=tcp"
    else:
        # No query parameters, add with ?
        return f"{rtsp_url}?transport=tcp"

####################################################################################################
# Detection configuration
####################################################################################################

# COCO class IDs that trigger parking spot "occupied" status
# These classes indicate a vehicle or large object is in the parking spot
CAR_STATUS_TRIGGER_CLASSES = [2, 8, 28]  # 2=car, 8=boat, 28=suitcase

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

def build_display_canvas(car_image, car_history, config, sensor=None, display_width=240, display_height=280):
    """Build complete display canvas with all overlays
    
    This function builds the complete image with statusbar, clock, temp/humidity, and video.
    Can be used for both display output and saving to file.
    
    Args:
        car_image: PIL Image (already processed with ROI if needed)
        car_history: List of car detection history
        config: Configuration object
        sensor: DHT22 sensor object (optional)
        display_width: Display width in pixels (default 240 for 1.69" LCD)
        display_height: Display height in pixels (default 280 for 1.69" LCD)
    
    Returns:
        PIL Image canvas with all overlays applied
    """
    try:
        font_path = get_font_path(config)
        # Use cached fonts to avoid reloading every frame (optimization)
        font = get_cached_font(font_path, 64, log)
        font_sm = get_cached_font(font_path, 32, log)
        canvas = Image.new("RGB", (display_width, display_height), (0, 0, 0))
        width, height = car_image.size
        
        # Check if we should fit full frame or use ROI
        use_full_frame = get_config_bool(config, 'ROI', 'use_full_frame', fallback=False)
        statusbar_height = 40 if get_config_bool(config, 'DETECTION', 'show_statusbar', fallback=True) else 0
        
        # Temperature and humidity panels are enabled if DHT22 sensor is available
        temp_enabled = sensor is not None
        humi_enabled = sensor is not None
        
        # Temp and humidity panels (side by side in the same row, overlayed on video)
        panel_width = display_width // 2  # Each panel takes half the screen width
        
        # Calculate panel heights based on font size + margins (cached for performance)
        # Cache key based on display dimensions and font sizes
        metrics_key = (display_width, display_height, font_path)
        if metrics_key not in _cached_font_metrics:
            temp_draw = ImageDraw.Draw(canvas)
            clock_margin = 8
            clock_bbox = temp_draw.textbbox((0, 0), "00:00", font=font)
            clock_font_height = clock_bbox[3] - clock_bbox[1]
            sensor_margin = 8
            sensor_bbox = temp_draw.textbbox((0, 0), "00.0ºC", font=font_sm)
            sensor_font_height = sensor_bbox[3] - sensor_bbox[1]
            _cached_font_metrics[metrics_key] = {
                'clock_font_height': clock_font_height,
                'sensor_font_height': sensor_font_height
            }
        
        metrics = _cached_font_metrics[metrics_key]
        clock_margin = 8
        clock_panel_height = metrics['clock_font_height'] + (clock_margin * 2)
        
        # Temp/Humidity panels: font size + margin top + margin bottom
        sensor_margin = 8  # Use max margin for consistency (temp uses 4, humidity uses 8)
        if temp_enabled or humi_enabled:
            sensor_panel_height = metrics['sensor_font_height'] + (sensor_margin * 2)
        else:
            sensor_panel_height = 0
        
        # Calculate video area: starts below clock, uses remaining space
        video_start_y = statusbar_height + clock_panel_height
        available_height = display_height - video_start_y  # Remaining height below clock
        available_width = display_width
        
        # Note: car_image is already processed (ROI extracted if needed) by prepare_display_image()
        # So we just need to scale and position it for display, regardless of use_full_frame setting
        # Fit to height: scale video to match available height, crop sides if needed
        scale = available_height / height
        new_height = available_height
        new_width = int(width * scale)
        
        # Resize maintaining aspect ratio
        # Use BILINEAR instead of LANCZOS for better performance on RPi (quality still good)
        car_image = car_image.resize((new_width, new_height), Image.Resampling.BILINEAR)
        
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
        draw_clock_panel(canvas, font, x=0, y=clock_y, width=display_width, height=clock_panel_height, bg_color=(127, 0, 127))
        
        # Draw temperature and humidity panels (side by side in the same row, overlayed on video)
        panel_y = video_start_y  # Start at video area (below clock)
        
        # Temperature panel (left half)
        if temp_enabled:
            draw_temp_panel(canvas, font_sm, x=0, y=panel_y, width=panel_width, height=sensor_panel_height, 
                          sensor=sensor, config=config)
        
        # Humidity panel (right half)
        if humi_enabled:
            draw_humi_panel(canvas, font_sm, x=panel_width, y=panel_y, width=panel_width, height=sensor_panel_height, 
                          sensor=sensor, config=config)

        return canvas
    except Exception as e:
        log.error(f"Error building display canvas: {e}")
        return None

def display_draw_status(disp, car_history, car_image, config, sensor=None):
    """Draw status on the display"""
    if disp is None:
        return
    
    try:
        # Build canvas using extracted function
        canvas = build_display_canvas(car_image, car_history, config, sensor, disp.width, disp.height)
        if canvas is not None:
            disp.ShowImage(canvas)
    except Exception as e:
        log.error(f"Error drawing display status: {e}")

def draw_clock_panel(img, font, x, y, width, height, bg_color=(127, 0, 127), text_color='white'):
    """Draw clock panel with centered text (full width across top of display)
    
    The colon blinks every second to indicate the clock is active.
    """
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
    
    # Center text vertically within panel with margins top and bottom
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
    """Draw temperature panel with centered text
    
    Background color interpolates from red (too cold) through green (ideal) to red (too hot)
    based on configured temperature thresholds.
    
    If sensor reading times out, displays last known value with a red stale indicator line.
    """
    global last_temp_value, last_temp_text, last_temp_color, temp_is_stale
    
    draw = ImageDraw.Draw(img)
    
    temp_text = None
    temp_color = None
    is_stale = False
    
    # Get temperature - use cached value if sensor times out
    try:
        temp = sensor.temperature
        if temp is not None:
            min_temp = config.getfloat('TEMPERATURE', 'min_value', fallback=16)
            ideal_temp = config.getfloat('TEMPERATURE', 'ideal_value', fallback=22)
            max_temp = config.getfloat('TEMPERATURE', 'max_value', fallback=28)
            temp_color = interpolate_color(temp, min_temp, ideal_temp, max_temp)
            temp_text = f"{temp:0.1f}ºC"
            # Update cache with fresh reading
            last_temp_value = temp
            last_temp_text = temp_text
            last_temp_color = temp_color
            temp_is_stale = False
            is_stale = False
    except RuntimeError:
        # Sensor reading timed out - use cached value if available
        if last_temp_text is not None:
            temp_text = last_temp_text
            temp_color = last_temp_color
            temp_is_stale = True
            is_stale = True
        else:
            log.debug('DHT temperature reading failed - no cached value available')
    except Exception as e:
        log.debug(f"Temperature display error: {e}")
        # Use cached value if available
        if last_temp_text is not None:
            temp_text = last_temp_text
            temp_color = last_temp_color
            temp_is_stale = True
            is_stale = True
    
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
    
    # Draw red stale indicator line at bottom of panel if data is stale
    if is_stale:
        line_y = y + height - 2  # At bottom of panel, 2px from edge (4px line will be inside)
        draw.line([(x, line_y), (x + width, line_y)], fill=(255, 0, 0), width=4)
    
    return img

def draw_humi_panel(img, font, x, y, width, height, sensor, config):
    """Draw humidity panel with centered text
    
    Background color interpolates from red (too dry/humid) through green (ideal) to red (too extreme)
    based on configured humidity thresholds.
    
    If sensor reading times out, displays last known value with a red stale indicator line.
    """
    global last_humi_value, last_humi_text, last_humi_color, humi_is_stale
    
    draw = ImageDraw.Draw(img)
    
    humi_text = None
    humi_color = None
    is_stale = False
    
    # Get humidity - use cached value if sensor times out
    try:
        humi = sensor.humidity
        if humi is not None:
            min_humi = config.getfloat('HUMIDITY', 'min_value', fallback=25)
            ideal_humi = config.getfloat('HUMIDITY', 'ideal_value', fallback=50)
            max_humi = config.getfloat('HUMIDITY', 'max_value', fallback=75)
            humi_color = interpolate_color(humi, min_humi, ideal_humi, max_humi)
            humi_text = f"{humi:0.1f}🌢%"
            # Update cache with fresh reading
            last_humi_value = humi
            last_humi_text = humi_text
            last_humi_color = humi_color
            humi_is_stale = False
            is_stale = False
    except RuntimeError:
        # Sensor reading timed out - use cached value if available
        if last_humi_text is not None:
            humi_text = last_humi_text
            humi_color = last_humi_color
            humi_is_stale = True
            is_stale = True
        else:
            log.debug('DHT humidity reading failed - no cached value available')
    except Exception as e:
        log.debug(f"Humidity display error: {e}")
        # Use cached value if available
        if last_humi_text is not None:
            humi_text = last_humi_text
            humi_color = last_humi_color
            humi_is_stale = True
            is_stale = True
    
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
    
    # Draw red stale indicator line at bottom of panel if data is stale
    if is_stale:
        line_y = y + height - 2  # At bottom of panel, 2px from edge (4px line will be inside)
        draw.line([(x, line_y), (x + width, line_y)], fill=(255, 0, 0), width=4)
    
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
    """Clean up display resources and properly shut down LCD screen
    
    Turns off backlight, clears screen, and releases hardware resources.
    Important for systemd services to ensure display is off on shutdown.
    """
    if disp is not None:
        try:
            log.info("Shutting down LCD display...")
            # Turn off backlight
            try:
                disp.bl_DutyCycle(0)
                time.sleep(0.05)  # Brief delay to ensure backlight turns off
            except Exception as e:
                log.debug(f"Error turning off backlight: {e}")
            
            # Clear screen to black (optional, but clean)
            try:
                # Create a black image to clear the display
                black_image = Image.new('RGB', (disp.width, disp.height), (0, 0, 0))
                disp.ShowImage(black_image)
                time.sleep(0.01)  # Brief delay to ensure image is displayed
            except Exception as e:
                log.debug(f"Error clearing screen: {e}")
            
            # Release hardware resources (GPIO, SPI)
            disp.module_exit()
            log.info("LCD display shut down successfully")
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
    """Draw status bar on display and log status to console
    
    The status bar shows green/red indicators for each frame in the car detection history,
    with green indicating car/suitcase detected and red indicating empty spot.
    """
    if display is not None:
        display_draw_status(display, car_history, debug_image, config, sensor)

    statusbar = ''
    for i, car_present in enumerate(car_history):
        statusbar = f'{statusbar}✔️ ' if car_present else f'{statusbar}❌ '
    log.debug(statusbar)

def overlay_bounding_boxes(image_pil, detections, roi_width, roi_height, config=None, class_names=None):
    """Overlay bounding boxes on an existing PIL image (YOLO detections)
    
    Args:
        image_pil: PIL Image to draw on
        detections: YOLO detection results
        roi_width: ROI width (unused, kept for compatibility)
        roi_height: ROI height (unused, kept for compatibility)
        config: Configuration object (optional)
        class_names: Dictionary mapping class IDs to names (optional, will use result.names if available)
    """
    if detections is None:
        return image_pil
    
    font_size = 48  # Doubled from 24 for better visibility
    font_path = get_font_path(config) if config else "assets/RobotoMonoMedium.ttf"
    # Use cached font to avoid reloading every frame (optimization)
    font = get_cached_font(font_path, font_size, log)

    outlinecolor = (0, 0, 0)
    draw = ImageDraw.Draw(image_pil)

    # Default class colors (for visual distinction)
    # Color palette for COCO classes - colors cycle through for classes beyond the palette size
    default_colors = [
        (255, 255, 0),    # Yellow
        (0, 0, 255),      # Blue - car (class 2)
        (255, 0, 0),      # Red
        (0, 255, 0),      # Green
        (255, 0, 255),    # Magenta
        (0, 255, 255),    # Cyan
    ]
    # Extended color palette for more classes
    extended_colors = [
        (255, 165, 0),    # Orange
        (128, 0, 128),    # Purple
        (255, 192, 203),  # Pink
        (0, 128, 128),    # Teal
        (255, 140, 0),    # Dark orange
        (75, 0, 130),     # Indigo
    ]
    all_colors = default_colors + extended_colors
    
    # Process YOLO results
    if detections is not None and len(detections) > 0:
        for result in detections:
            boxes = result.boxes
            if len(boxes) > 0:
                # Get class names from result object if available (Ultralytics YOLO provides this)
                result_class_names = None
                if hasattr(result, 'names') and result.names:
                    result_class_names = result.names
                elif class_names:
                    result_class_names = class_names
                
                for i in range(len(boxes)):
                    confidence = float(boxes.conf[i].item() if hasattr(boxes.conf[i], 'item') else boxes.conf[i])
                    class_id = int(boxes.cls[i].item() if hasattr(boxes.cls[i], 'item') else boxes.cls[i])
                    
                    # Get bounding box coordinates
                    xyxy = boxes.xyxy[i].cpu().numpy() if hasattr(boxes.xyxy[i], 'cpu') else boxes.xyxy[i].numpy()
                    x1, y1, x2, y2 = xyxy
                    startX, startY, endX, endY = int(x1), int(y1), int(x2), int(y2)
                    
                    # Get class name from result's names dictionary, or fallback
                    if result_class_names and class_id in result_class_names:
                        class_name = result_class_names[class_id]
                    elif result_class_names and isinstance(result_class_names, (list, tuple)) and class_id < len(result_class_names):
                        class_name = result_class_names[class_id]
                    else:
                        # Fallback to generic class name
                        class_name = f"Class {class_id}"
                    
                    label = f"[{class_id}] {class_name}: {confidence:.2f}"
                    
                    # Assign colors based on class type:
                    # Green for car status triggers (classes that indicate parking spot occupied)
                    # Yellow for people (person class 0)
                    # Purple for everything else
                    if class_id in CAR_STATUS_TRIGGER_CLASSES:
                        class_color = (0, 255, 0)  # Green
                    elif class_id == 0:  # Person
                        class_color = (255, 255, 0)  # Yellow
                    else:  # All other classes
                        class_color = (128, 0, 128)  # Purple
                    
                    # Draw bounding box
                    draw.rectangle([(startX, startY), (endX, endY)], outline=class_color, width=2)
                    
                    # Draw text with outline (better quality for small screens)
                    outline_width = max(1, font_size // 16)  # Proportional outline width
                    captionX = startX + outline_width
                    captionY = endY - font_size - outline_width
                    
                    # Try using PIL's built-in stroke support (Pillow 8.0.0+)
                    # This creates a smooth, professional outline
                    try:
                        draw.text((captionX, captionY), label, font=font, fill=class_color,
                                 stroke_width=outline_width, stroke_fill=outlinecolor)
                    except TypeError:
                        # Fallback for older PIL versions: draw outline in 8 directions
                        # This creates a smoother outline than the previous 4-offset method
                        offsets = [(-1,-1), (0,-1), (1,-1), (-1,0), (1,0), (-1,1), (0,1), (1,1)]
                        for dx, dy in offsets:
                            draw.text((captionX + dx * outline_width, captionY + dy * outline_width), 
                                     label, font=font, fill=outlinecolor)
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
    """Create a placeholder image when video source is not available
    
    Used when RTSP stream is disconnected or local video file cannot be read.
    """
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

# Parse command-line arguments
parser = argparse.ArgumentParser(description='Parking camera monitoring system')
parser.add_argument('--save-frame', '-s', action='store_true',
                    help='Capture frame(s), process with bounding boxes, and save to disk (exits after save)')
parser.add_argument('--count', '-c', type=int, default=1, metavar='N',
                    help='Number of frames to capture when using --save-frame (default: 1)')
parser.add_argument('--interval', '-t', type=float, default=0.0, metavar='SECONDS',
                    help='Time interval in seconds between frame captures when using --save-frame (default: 0)')
parser.add_argument('--output', '-o', type=str, default='.',
                    help='Output path for --save-frame (directory path, relative or absolute; default: current working directory). Filename is always snapshot_<timestamp>.png')
args = parser.parse_args()

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

# Initialize display (skip in save-frame mode for faster startup)
display = None
if not args.save_frame:
    display = display_init()
    if display is None:
        log.warning("Display initialization failed. Continuing without display.")
else:
    log.info("Skipping display initialization in save-frame mode")

# Initialize DHT22 sensor (skip in save-frame mode to avoid conflicts with background process)
sensor = None
if not args.save_frame:
    try:
        sensor = adafruit_dht.DHT22(board.D4)
        log.info("DHT22 sensor initialized")
    except Exception as e:
        log.warning(f"Failed to initialize DHT22 sensor: {e}. Continuing without sensor.")
else:
    log.info("Skipping sensor initialization in save-frame mode")

# Sensor reading cache to prevent flicker on timeout
last_temp_value = None
last_temp_text = None
last_temp_color = None
temp_is_stale = False

last_humi_value = None
last_humi_text = None
last_humi_color = None
humi_is_stale = False

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

# Get ROI configuration
roi_method = config.get('ROI', 'roi_method', fallback='point_quadrant').strip().lower()
use_full_frame = get_config_bool(config, 'ROI', 'use_full_frame', fallback=False)

# ROI configuration based on method
if roi_method == 'coordinates':
    # Use explicit coordinates (x, y, width, height)
    roi_x = config.getint('ROI', 'x', fallback=800)
    roi_y = config.getint('ROI', 'y', fallback=500)
    roi_w = config.getint('ROI', 'width', fallback=550)
    roi_h = config.getint('ROI', 'height', fallback=580)
    roi_point_x = None
    roi_point_y = None
    roi_quadrant = None
    log.info(f"ROI method: coordinates - x={roi_x}, y={roi_y}, width={roi_w}, height={roi_h}")
else:
    # Use point + quadrant (default or if method is 'point_quadrant')
    roi_point_x = config.getint('ROI', 'point_x', fallback=450)
    roi_point_y = config.getint('ROI', 'point_y', fallback=400)
    roi_quadrant = config.getint('ROI', 'quadrant', fallback=4)
    roi_x = None
    roi_y = None
    roi_w = None
    roi_h = None
    log.info(f"ROI method: point_quadrant - point=({roi_point_x},{roi_point_y}), quadrant={roi_quadrant}")

# ROI will be calculated from point + quadrant when we have frame dimensions
# Function to calculate ROI coordinates from point + quadrant
def calculate_roi_from_point_quadrant(point_x, point_y, quadrant, frame_width, frame_height):
    """Calculate ROI (x, y, width, height) from point and quadrant
    
    Quadrants:
    1 = Top right: from point to top-right corner (point is bottom-left of ROI)
    2 = Top left: from point to top-left corner (point is bottom-right of ROI)
    3 = Bottom left: from point to bottom-left corner (point is top-right of ROI)
    4 = Bottom right: from point to bottom-right corner (point is top-left of ROI)
    
    Args:
        point_x, point_y: Point coordinates
        quadrant: Quadrant number (1-4)
        frame_width, frame_height: Frame dimensions
    
    Returns:
        Tuple of (x, y, width, height) for ROI
    """
    if quadrant == 1:  # Top right
        x = point_x
        y = 0
        width = frame_width - point_x
        height = point_y
    elif quadrant == 2:  # Top left
        x = 0
        y = 0
        width = point_x
        height = point_y
    elif quadrant == 3:  # Bottom left
        x = 0
        y = point_y
        width = point_x
        height = frame_height - point_y
    elif quadrant == 4:  # Bottom right
        x = point_x
        y = point_y
        width = frame_width - point_x
        height = frame_height - point_y
    else:
        log.warning(f"Invalid quadrant {quadrant}, defaulting to quadrant 4 (bottom right)")
        x = point_x
        y = point_y
        width = frame_width - point_x
        height = frame_height - point_y
    
    # Ensure valid dimensions
    x = max(0, min(x, frame_width - 1))
    y = max(0, min(y, frame_height - 1))
    width = max(1, min(width, frame_width - x))
    height = max(1, min(height, frame_height - y))
    
    return x, y, width, height

# ROI coordinates are now set above based on roi_method
# If using point_quadrant, roi_x/roi_y/roi_w/roi_h will be None initially and calculated dynamically
# If using coordinates, roi_x/roi_y/roi_w/roi_h are set directly from config

cv_interval = config.getfloat('DETECTION', 'cv_interval', fallback=1.0)
confidence_threshold = config.getfloat('DETECTION', 'confidence_threshold', fallback=0.4)
history_size = config.getint('DETECTION', 'history_size', fallback=120)
car_present_threshold = config.getint('DETECTION', 'car_present_threshold', fallback=80)
car_absent_threshold = config.getint('DETECTION', 'car_absent_threshold', fallback=40)

# Temporal smoothing window: keep car detected for N detection cycles after last successful detection
# Number of whole detection cycles to persist detection after last success
# Example: 1 = keep detected for 1 cycle after last success (covers 1 missed detection)
temporal_smoothing_cycles = config.getint('DETECTION', 'temporal_smoothing_cycles', fallback=1)
temporal_smoothing_window = cv_interval * temporal_smoothing_cycles
log.info(f"Temporal smoothing: {temporal_smoothing_window:.1f}s window ({temporal_smoothing_cycles} cycle(s) x cv_interval={cv_interval:.1f}s)")

# Parking spot detection state
last_cv_time = time.time()  # Timestamp of last CV processing run
car_history = []  # History buffer of detection results (True=car/suitcase detected, False=empty)

# Shared state for display and CV results (updated by CV thread, read by display loop)
latest_detections = None  # YOLO detection results for bounding box overlay
latest_detection_frame_size = None  # (width, height) tuple for coordinate scaling
cv_processing = False  # Flag to prevent multiple concurrent CV processing threads
display_lock = None

# Frame caching for static scenes (detect identical frames)
last_frame_hash = None
last_frame_detection_result = None  # (car_detected, detections, frame_size)
last_frame_hash_time = None

# Temporal smoothing for missed detections
last_car_detection_time = None  # Timestamp of last successful car detection (initialized above)
try:
    import threading
    display_lock = threading.Lock()
    HAS_THREADING = True
except ImportError:
    HAS_THREADING = False

# Font cache to avoid reloading fonts on every frame (optimization for RPi)
_font_cache = {}

def get_cached_font(font_path, size, logger=None):
    """Get font from cache or load it if not cached (optimization for RPi)"""
    cache_key = (font_path, size)
    if cache_key not in _font_cache:
        _font_cache[cache_key] = load_font(font_path, size, logger)
    return _font_cache[cache_key]

# Cached font metrics for display calculations (calculated once per display size change)
_cached_font_metrics = {}


def compute_frame_hash(frame):
    """Compute a fast hash of frame for similarity detection
    
    Uses a downsampled grayscale version for speed.
    Returns None if frame is None.
    
    Args:
        frame: OpenCV frame (numpy array)
    
    Returns:
        String hash of frame, or None if frame is None or error occurs
    """
    if frame is None:
        return None
    
    try:
        # Convert to grayscale and downsample for fast hashing
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        small = cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA)
        # Use hash of flattened array
        frame_bytes = small.tobytes()
        return hashlib.md5(frame_bytes).hexdigest()
    except Exception as e:
        log.debug(f"Error computing frame hash: {e}")
        return None

def process_cv_detection(frame, use_full_frame, roi_x, roi_y, roi_w, roi_h,
                         yolo_model, confidence_threshold, 
                         last_frame_hash=None, last_frame_detection_result=None,
                         last_car_detection_time=None, temporal_smoothing_window=3.0,
                         roi_point_x=None, roi_point_y=None, roi_quadrant=None):
    """Process a frame with YOLO detection (runs in separate thread or at intervals)
    
    Detects objects in CAR_STATUS_TRIGGER_CLASSES (currently car and suitcase) in the frame or ROI.
    Only these classes trigger the parking spot "occupied" status.
    
    Implements hybrid caching approach:
    - Frame hash comparison: If frame is identical to last processed frame, reuse cached detection
    - Temporal smoothing: If frame is different but car was detected recently, keep it detected
    
    Args:
        frame: OpenCV frame (numpy array)
        use_full_frame: If True, use entire frame; if False, extract ROI first
        roi_x, roi_y, roi_w, roi_h: Region of Interest coordinates (only used if use_full_frame=False)
        yolo_model: YOLOv11 model instance
        confidence_threshold: Minimum confidence for detections (0.0-1.0)
        last_frame_hash: Hash of last processed frame (for caching)
        last_frame_detection_result: Cached (car_detected, detections, frame_size) tuple
        last_car_detection_time: Timestamp of last successful car detection (for temporal smoothing)
        temporal_smoothing_window: Time window in seconds for temporal smoothing (calculated as cv_interval * cycles)
    
    Returns:
        Tuple of (car_detected, detections, frame_size, frame_hash):
        - car_detected: Boolean indicating if car/suitcase was detected
        - detections: YOLO detection results (for bounding box overlay)
        - frame_size: (width, height) tuple of the detection region
        - frame_hash: Hash of processed frame for next comparison
    """
    try:
        # Determine what region to use for detection
        if use_full_frame:
            # Use entire frame for detection
            detection_frame = frame.copy()  # Copy to avoid issues if frame is modified
            frame_h, frame_w = frame.shape[:2]
            frame_size = (frame_w, frame_h)
        else:
            # Extract ROI from frame (with bounds validation)
            frame_h, frame_w = frame.shape[:2]
            
            # Calculate ROI from point + quadrant if not already calculated
            if roi_x is None and roi_point_x is not None and roi_point_y is not None and roi_quadrant is not None:
                roi_x, roi_y, roi_w, roi_h = calculate_roi_from_point_quadrant(
                    roi_point_x, roi_point_y, roi_quadrant, frame_w, frame_h
                )
                log.debug(f"Calculated ROI from point ({roi_point_x},{roi_point_y}) + quadrant {roi_quadrant}: ({roi_x},{roi_y},{roi_w},{roi_h})")
            
            # Store original ROI values for logging
            original_roi_x, original_roi_y, original_roi_w, original_roi_h = roi_x, roi_y, roi_w, roi_h
            # Validate and clamp ROI bounds to ensure they fit within frame
            # Clamp ROI width/height first to frame dimensions, then clamp position
            roi_w = max(1, min(roi_w, frame_w))  # Ensure at least 1 pixel wide
            roi_h = max(1, min(roi_h, frame_h))  # Ensure at least 1 pixel tall
            # Clamp position to ensure ROI fits within frame
            roi_x = max(0, min(roi_x, frame_w - roi_w))
            roi_y = max(0, min(roi_y, frame_h - roi_h))
            # Log warning if bounds were adjusted
            if roi_x != original_roi_x or roi_y != original_roi_y or roi_w != original_roi_w or roi_h != original_roi_h:
                log.warning(f"ROI bounds adjusted in CV: frame={frame_w}x{frame_h}, original=({original_roi_x},{original_roi_y},{original_roi_w},{original_roi_h}), clamped=({roi_x},{roi_y},{roi_w},{roi_h})")
            
            detection_frame = frame[roi_y:roi_y+roi_h, roi_x:roi_x+roi_w].copy()
            frame_h, frame_w = roi_h, roi_w
            frame_size = (frame_w, frame_h)
        
        # Compute hash of current frame for caching
        current_frame_hash = compute_frame_hash(detection_frame)
        
        # HYBRID APPROACH #1: Frame hash comparison - reuse cached result if frame is identical
        if (current_frame_hash is not None and 
            last_frame_hash is not None and 
            current_frame_hash == last_frame_hash and
            last_frame_detection_result is not None):
            car_detected, detections, cached_frame_size = last_frame_detection_result
            # Verify frame size still matches
            if cached_frame_size == frame_size:
                log.debug("Frame unchanged - reusing cached detection result")
                return car_detected, detections, frame_size, current_frame_hash
        
        # Frame is different or no cache - run detection
        car_detected = False
        detections = None
        
        # YOLO detection (COCO classes)
        # Check for classes defined in CAR_STATUS_TRIGGER_CLASSES for parking spot occupancy
        if yolo_model is not None:
            # Run YOLO detection (imgsz auto-detected from frame size for optimal performance)
            results = yolo_model(detection_frame, conf=confidence_threshold, verbose=False)
            detections = results
            
            # Check for classes that trigger parking spot occupancy (early exit for performance)
            for result in results:
                boxes = result.boxes
                if len(boxes) > 0:
                    # Convert to numpy once for better performance
                    if hasattr(boxes.cls, 'cpu'):
                        class_ids = boxes.cls.cpu().numpy()
                    else:
                        class_ids = boxes.cls.numpy()
                    
                    # Check if any detected class is in trigger classes (vectorized check)
                    if any(int(cid) in CAR_STATUS_TRIGGER_CLASSES for cid in class_ids):
                        car_detected = True
                        break
        
        # HYBRID APPROACH #2: Temporal smoothing - if detection failed but car was detected recently, keep it
        if not car_detected and last_car_detection_time is not None:
            current_time = time.time()
            time_since_last_detection = current_time - last_car_detection_time
            if time_since_last_detection < temporal_smoothing_window:
                log.debug(f"Temporal smoothing: keeping car detected (last detection {time_since_last_detection:.1f}s ago)")
                car_detected = True
                # Note: We still return the current detections (might be None), not cached ones
                # This keeps the bounding boxes from disappearing immediately
        
        return car_detected, detections, frame_size, current_frame_hash
    except Exception as e:
        log.error(f"Error processing frame with CV: {e}")
        if use_full_frame:
            frame_h, frame_w = frame.shape[:2] if frame is not None else (480, 640)
        else:
            frame_w, frame_h = roi_w, roi_h
        return False, None, (frame_w, frame_h), None

def prepare_display_image(frame, use_full_frame, roi_x, roi_y, roi_w, roi_h, config=None,
                          roi_point_x=None, roi_point_y=None, roi_quadrant=None):
    """Prepare frame for display (without CV processing)
    
    Extracts ROI if needed and converts OpenCV BGR format to PIL RGB format.
    Performs bounds validation and clamping to ensure ROI fits within frame.
    If roi_x is None, calculates ROI from point + quadrant based on frame dimensions.
    
    Args:
        frame: OpenCV frame (numpy array) or None
        use_full_frame: If True, use entire frame; if False, extract ROI
        roi_x, roi_y, roi_w, roi_h: Region of Interest coordinates (None if not yet calculated)
        config: Configuration object (optional)
        roi_point_x, roi_point_y, roi_quadrant: ROI point and quadrant (used if roi_x is None)
    
    Returns:
        PIL Image ready for display, or placeholder image if frame is None
    """
    if frame is None:
        if use_full_frame:
            return create_placeholder_image(640, 480, config)
        else:
            # For placeholder, use default ROI size if not calculated yet
            if roi_w is None or roi_h is None:
                return create_placeholder_image(550, 580, config)
            return create_placeholder_image(roi_w, roi_h, config)
    
    try:
        if use_full_frame:
            # Convert full frame to PIL Image
            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            return Image.fromarray(frame_rgb)
        else:
            # Extract ROI and convert to PIL Image (with bounds validation)
            frame_h, frame_w = frame.shape[:2]
            
            # Calculate ROI from point + quadrant if not already calculated
            if roi_x is None and roi_point_x is not None and roi_point_y is not None and roi_quadrant is not None:
                roi_x, roi_y, roi_w, roi_h = calculate_roi_from_point_quadrant(
                    roi_point_x, roi_point_y, roi_quadrant, frame_w, frame_h
                )
                log.debug(f"Calculated ROI from point ({roi_point_x},{roi_point_y}) + quadrant {roi_quadrant}: ({roi_x},{roi_y},{roi_w},{roi_h})")
            
            # Store original ROI values for logging
            original_roi_x, original_roi_y, original_roi_w, original_roi_h = roi_x, roi_y, roi_w, roi_h
            # Validate and clamp ROI bounds to ensure they fit within frame
            # Clamp ROI width/height first to frame dimensions, then clamp position
            roi_w = max(1, min(roi_w, frame_w))  # Ensure at least 1 pixel wide
            roi_h = max(1, min(roi_h, frame_h))  # Ensure at least 1 pixel tall
            # Clamp position to ensure ROI fits within frame
            roi_x = max(0, min(roi_x, frame_w - roi_w))
            roi_y = max(0, min(roi_y, frame_h - roi_h))
            # Log warning if bounds were adjusted
            if roi_x != original_roi_x or roi_y != original_roi_y or roi_w != original_roi_w or roi_h != original_roi_h:
                log.warning(f"ROI bounds adjusted in display: frame={frame_w}x{frame_h}, original=({original_roi_x},{original_roi_y},{original_roi_w},{original_roi_h}), clamped=({roi_x},{roi_y},{roi_w},{roi_h})")
            
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
                        car_history, history_size, car_present_threshold, car_absent_threshold,
                        roi_point_x=None, roi_point_y=None, roi_quadrant=None):
    """Background thread function for CV processing
    
    Runs YOLO detection on a frame and updates shared state:
    - latest_detections: Detection results for bounding box overlay
    - latest_detection_frame_size: Size of the detection region
    - car_history: History buffer of car detection results (thread-safe)
    - Frame caching and temporal smoothing state for hybrid approach
    
    Note: car_present_threshold and car_absent_threshold are not used here,
    they are applied later when evaluating the car_history buffer.
    """
    global latest_detections, latest_detection_frame_size, cv_processing
    global last_frame_hash, last_frame_detection_result, last_frame_hash_time
    global last_car_detection_time, temporal_smoothing_window
    
    try:
        # Get current cache state (thread-safe read)
        if display_lock:
            with display_lock:
                current_last_hash = last_frame_hash
                current_last_result = last_frame_detection_result
                current_last_detection_time = last_car_detection_time
        else:
            current_last_hash = last_frame_hash
            current_last_result = last_frame_detection_result
            current_last_detection_time = last_car_detection_time
        
        car_detected, detections, frame_size, frame_hash = process_cv_detection(
            frame, use_full_frame, roi_x, roi_y, roi_w, roi_h,
            yolo_model, confidence_threshold,
            last_frame_hash=current_last_hash,
            last_frame_detection_result=current_last_result,
            last_car_detection_time=current_last_detection_time,
            temporal_smoothing_window=temporal_smoothing_window,
            roi_point_x=roi_point_x, roi_point_y=roi_point_y, roi_quadrant=roi_quadrant
        )
        
        # Update frame cache and temporal smoothing state
        if frame_hash is not None:
            if display_lock:
                with display_lock:
                    last_frame_hash = frame_hash
                    last_frame_detection_result = (car_detected, detections, frame_size)
                    last_frame_hash_time = time.time()
                    # Update temporal smoothing timestamp if car was detected
                    if car_detected:
                        last_car_detection_time = time.time()
            else:
                last_frame_hash = frame_hash
                last_frame_detection_result = (car_detected, detections, frame_size)
                last_frame_hash_time = time.time()
                if car_detected:
                    last_car_detection_time = time.time()
        
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

# If save-frame mode, capture frame(s) and exit (no display hardware needed)
if args.save_frame:
    frame_count = max(1, args.count)  # Ensure at least 1 frame
    interval = max(0.0, args.interval)  # Ensure non-negative interval
    
    if frame_count == 1:
        log.info("Running in save-frame mode (single capture)...")
    else:
        log.info(f"Running in save-frame mode ({frame_count} frames, {interval}s interval)...")
    
    # Use display dimensions from config or defaults (240x280 for 1.69" LCD)
    # Display initialization is optional in save mode
    display_width = display.width if display and hasattr(display, 'width') else 240
    display_height = display.height if display and hasattr(display, 'height') else 280
    
    # Set read timeout before reading (RTSP can block indefinitely otherwise)
    if rtsp_url is not None and cap is not None:
        timeout_ms = rtsp_timeout * 1000
        cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, timeout_ms)
        os.environ['OPENCV_FFMPEG_READ_TIMEOUT_MSEC'] = str(timeout_ms)
    
    saved_count = 0
    
    # Capture specified number of frames
    if cap is not None:
        for frame_num in range(1, frame_count + 1):
            try:
                if frame_count > 1:
                    log.info(f"Capturing frame {frame_num}/{frame_count}...")
                else:
                    log.info("Setting read timeout and attempting to capture frame...")
                
                # Read frame
                with suppress_stderr():
                    log.info("Reading frame from video source...")
                    ret, frame = cap.read()
            except KeyboardInterrupt:
                log.info(f"Interrupted by user during frame capture (saved {saved_count} frame(s))")
                if cap is not None:
                    cap.release()
                display_exit(display)
                sys.exit(0)
            except Exception as e:
                log.error(f"Error reading frame: {e}")
                ret = False
                frame = None
            
            if ret and frame is not None:
                log.info(f"Frame {frame_num} captured successfully ({frame.shape[1]}x{frame.shape[0]})")
                # Prepare base image (ROI extraction if needed)
                display_image = prepare_display_image(frame, use_full_frame, roi_x, roi_y, roi_w, roi_h, config=config,
                                                      roi_point_x=roi_point_x, roi_point_y=roi_point_y, roi_quadrant=roi_quadrant)
                
                # Run CV detection synchronously for bounding boxes
                log.info("Running YOLO detection...")
                try:
                    # In save-frame mode, we don't use caching (fresh detection every time)
                    car_detected, detections, frame_size, _ = process_cv_detection(
                        frame, use_full_frame, roi_x, roi_y, roi_w, roi_h,
                        yolo_model, confidence_threshold,
                        last_frame_hash=None,
                        last_frame_detection_result=None,
                        last_car_detection_time=None,
                        temporal_smoothing_window=0.0,  # Disable temporal smoothing in save-frame mode
                        roi_point_x=roi_point_x, roi_point_y=roi_point_y, roi_quadrant=roi_quadrant
                    )
                    log.info(f"Detection complete: car_detected={car_detected}")
                except KeyboardInterrupt:
                    log.info(f"Interrupted by user during detection (saved {saved_count} frame(s))")
                    if cap is not None:
                        cap.release()
                    display_exit(display)
                    sys.exit(0)
                except Exception as e:
                    log.error(f"Error during detection: {e}")
                    car_detected = False
                    detections = None
                    frame_size = None
                
                # Overlay bounding boxes if available (save-frame mode: just frame + bounding boxes, no UI)
                if detections is not None and frame_size is not None:
                    display_w, display_h = display_image.size
                    det_w, det_h = frame_size
                    if display_w == det_w and display_h == det_h:
                        class_names = yolo_model.names if yolo_model and hasattr(yolo_model, 'names') else None
                        display_image = overlay_bounding_boxes(
                            display_image, detections, display_w, display_h, config=config, class_names=class_names
                        )
                
                # Save the image with bounding boxes (no statusbar, clock, or temp/humidity)
                # Generate output filename (always snapshot_<timestamp>.png)
                timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                filename = f"snapshot_{timestamp}.png"
                
                # args.output can be a relative or absolute path
                output_dir = args.output
                
                # Create output directory if needed
                os.makedirs(output_dir, exist_ok=True)
                
                # Construct full path
                output_path = os.path.join(output_dir, filename)
                
                # Save the frame with bounding boxes
                log.info(f"Saving frame {frame_num} to: {output_path}")
                display_image.save(output_path, "PNG")
                log.info(f"Saved frame {frame_num} to: {output_path}")
                saved_count += 1
            else:
                log.error(f"Failed to read frame {frame_num} for saving (timeout or stream error)")
            
            # Wait for interval before next capture (skip on last frame)
            if frame_num < frame_count and interval > 0:
                log.info(f"Waiting {interval} seconds before next capture...")
                try:
                    time.sleep(interval)
                except KeyboardInterrupt:
                    log.info(f"Interrupted during wait (saved {saved_count} frame(s))")
                    if cap is not None:
                        cap.release()
                    display_exit(display)
                    sys.exit(0)
    else:
        log.error("No video source available for frame capture")
    
    # Cleanup and exit
    log.info(f"Completed: saved {saved_count}/{frame_count} frame(s). Cleaning up and exiting...")
    if cap is not None:
        cap.release()
    display_exit(display)
    sys.exit(0)

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
                      car_history, history_size, car_present_threshold, car_absent_threshold,
                      roi_point_x, roi_point_y, roi_quadrant),
                daemon=True
            )
            thread.start()
        elif cv_should_run and frame is not None and ret and not HAS_THREADING:
            # Fallback: run CV synchronously if threading not available (not ideal)
            last_cv_time = current_time
            
            # Get current cache state (thread-safe read)
            if display_lock:
                with display_lock:
                    current_last_hash = last_frame_hash
                    current_last_result = last_frame_detection_result
                    current_last_detection_time = last_car_detection_time
            else:
                current_last_hash = last_frame_hash
                current_last_result = last_frame_detection_result
                current_last_detection_time = last_car_detection_time
            
            car_detected, detections, frame_size, frame_hash = process_cv_detection(
                frame, use_full_frame, roi_x, roi_y, roi_w, roi_h,
                yolo_model, confidence_threshold,
                last_frame_hash=current_last_hash,
                last_frame_detection_result=current_last_result,
                last_car_detection_time=current_last_detection_time,
                temporal_smoothing_window=temporal_smoothing_window,
                roi_point_x=roi_point_x, roi_point_y=roi_point_y, roi_quadrant=roi_quadrant
            )
            
            # Update frame cache and temporal smoothing state
            if frame_hash is not None:
                if display_lock:
                    with display_lock:
                        last_frame_hash = frame_hash
                        last_frame_detection_result = (car_detected, detections, frame_size)
                        last_frame_hash_time = time.time()
                        if car_detected:
                            last_car_detection_time = time.time()
                else:
                    last_frame_hash = frame_hash
                    last_frame_detection_result = (car_detected, detections, frame_size)
                    last_frame_hash_time = time.time()
                    if car_detected:
                        last_car_detection_time = time.time()
            
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
            display_image = prepare_display_image(frame, use_full_frame, roi_x, roi_y, roi_w, roi_h, config=config,
                                                  roi_point_x=roi_point_x, roi_point_y=roi_point_y, roi_quadrant=roi_quadrant)
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
        # Size matching ensures bounding boxes align correctly with the displayed image
        if current_detections is not None and current_detection_frame_size is not None:
            # Get display image dimensions
            display_w, display_h = display_image.size
            det_w, det_h = current_detection_frame_size
            
            # Only overlay if dimensions match (ensures bounding boxes align correctly)
            if display_w == det_w and display_h == det_h:
                # Pass model's class names if available
                class_names = yolo_model.names if yolo_model and hasattr(yolo_model, 'names') else None
                display_image = overlay_bounding_boxes(
                    display_image, current_detections, display_w, display_h, config=config, class_names=class_names
                )
        
        # Display the image with statusbar (thread-safe read of car_history)
        if display_lock:
            with display_lock:
                current_car_history = car_history.copy()  # Copy for thread safety
        else:
            current_car_history = car_history
        draw_statusbar(current_car_history, display_image, config, display, sensor)

        # Small sleep to prevent tight loop (reduced to ~15fps for better RPi performance)
        # 15fps is sufficient for status display and reduces CPU load significantly
        time.sleep(0.067)  # ~15fps (optimized for RPi)

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
