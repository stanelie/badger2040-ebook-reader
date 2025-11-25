import board
import digitalio
import storage
import time

# Set up button A on the Badger 2040
# In CircuitPython, button A is available as board.SW_A
button_a = digitalio.DigitalInOut(board.SW_A)
button_a.direction = digitalio.Direction.INPUT
button_a.pull = digitalio.Pull.DOWN

# Give the button state time to stabilize
# This is important for reliable detection during boot
time.sleep(0.1)

# Read the button state
button_pressed = button_a.value

# Clean up the button pin
button_a.deinit()

# Check if button A is pressed (active high with pull-down)
# If pressed, disable USB mass storage
if button_pressed:
    # Disable USB mass storage
    # This makes the CIRCUITPY drive invisible to the host computer
    # allowing CircuitPython to write to the filesystem
    storage.disable_usb_drive()
    print("USB mass storage DISABLED - button A was held during boot")
else:
    print("USB mass storage ENABLED - button A was not pressed")