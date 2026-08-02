# ------------------------------------------------------------
# factory.py  -  wipe saved state and restart
# ------------------------------------------------------------
# Split out of code.py because it runs once in a device's lifetime - a
# ten-second hold on A - and code.py is compiled into RAM at every boot and
# stays there for the whole session. Imported at the point of use, this costs
# nothing until the moment it is wanted.
#
# Reaches the reader through __main__ the way convert_ui and coverimg do.
import os
import time

import microcontroller


def reset():
    """Clear NVRAM and reboot. Does not return."""
    try:
        import __main__ as reader
    except ImportError:
        import sys
        reader = sys.modules.get("__main__")

    reader.show_message(("RESETTING...", 80, 55))

    try:
        microcontroller.nvm[0:256] = bytes(256)
        print("NVRAM cleared")
    except Exception as e:
        print(f"NVRAM clear error: {e}")

    # Left over from a version that kept page indexes on the filesystem.
    try:
        for f in os.listdir("/state"):
            if f.endswith(".idx"):
                try:
                    os.remove("/state/" + f)
                    print(f"Deleted: /state/{f}")
                except Exception:
                    pass
    except Exception:
        pass

    reader.show_message(("RESET COMPLETE", 65, 45), ("Restarting...", 80, 70))
    time.sleep(1.5)
    microcontroller.reset()
