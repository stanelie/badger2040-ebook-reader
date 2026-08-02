// This file is part of the CircuitPython project: https://circuitpython.org
//
// SPDX-FileCopyrightText: Copyright (c) 2024 Scott Shawcroft for Adafruit Industries
//
// SPDX-License-Identifier: MIT

#include "py/obj.h"
#include "py/runtime.h"
#include "py/mphal.h"

#include "badger-shared.h"

//| """Badger 2040 hardware support
//|
//| The `badger2040` module provides hardware-specific functions for the
//| Pimoroni Badger 2040, including fast wake button detection and power control.
//|
//| Example usage::
//|
//|     import badger2040
//|
//|     # Check if woken by any button
//|     if badger2040.woken_by_button():
//|         # Check which specific button
//|         if badger2040.pressed_to_wake(badger2040.BUTTON_A):
//|             print("Woken by button A!")
//|
//|     # Do work here...
//|
//|     # Halt to save power (turns off on battery, continues on USB)
//|     badger2040.halt()
//| """

//| BUTTON_A: int
//| """Pin number for button A (12)"""
//|
//| BUTTON_B: int
//| """Pin number for button B (13)"""
//|
//| BUTTON_C: int
//| """Pin number for button C (14)"""
//|
//| BUTTON_UP: int
//| """Pin number for the up button (15)"""
//|
//| BUTTON_DOWN: int
//| """Pin number for the down button (11)"""
//|
//| BUTTON_USER: int
//| """Pin number for the user button (23)"""

//| def woken_by_button() -> bool:
//|     """Check if the device was woken by any button press.
//|
//|     This function checks button state captured at the earliest possible
//|     moment during boot, before CircuitPython fully initializes. This allows
//|     detection of short button presses that would otherwise be missed.
//|
//|     :return: True if any button was pressed during wake, False otherwise.
//|     """
//|     ...
//|
static mp_obj_t badger2040_woken_by_button(void) {
    return mp_obj_new_bool(board_woken_by_button());
}
static MP_DEFINE_CONST_FUN_OBJ_0(badger2040_woken_by_button_obj, badger2040_woken_by_button);

//| def pressed_to_wake(button: int) -> bool:
//|     """Check if a specific button was pressed to wake the device.
//|
//|     This performs a non-destructive read of the wake state for the
//|     specified button.
//|
//|     :param button: The button pin number (use BUTTON_A, BUTTON_B, etc.)
//|     :return: True if the button was pressed during wake, False otherwise.
//|     """
//|     ...
//|
static mp_obj_t badger2040_pressed_to_wake(mp_obj_t button_obj) {
    uint8_t button = mp_obj_get_int(button_obj);
    return mp_obj_new_bool(board_button_pressed_to_wake(button));
}
static MP_DEFINE_CONST_FUN_OBJ_1(badger2040_pressed_to_wake_obj, badger2040_pressed_to_wake);

//| def pressed_to_wake_get_once(button: int) -> bool:
//|     """Check if a specific button was pressed to wake, then clear its state.
//|
//|     This is useful when you want to handle a button press only once,
//|     preventing re-triggering on the same press. After calling this,
//|     subsequent calls for the same button will return False until the
//|     next boot.
//|
//|     :param button: The button pin number (use BUTTON_A, BUTTON_B, etc.)
//|     :return: True if the button was pressed during wake (first call only).
//|     """
//|     ...
//|
static mp_obj_t badger2040_pressed_to_wake_get_once(mp_obj_t button_obj) {
    uint8_t button = mp_obj_get_int(button_obj);
    return mp_obj_new_bool(board_button_wake_get_once(button));
}
static MP_DEFINE_CONST_FUN_OBJ_1(badger2040_pressed_to_wake_get_once_obj, badger2040_pressed_to_wake_get_once);

//| def reset_pressed_to_wake() -> None:
//|     """Clear all wake button state.
//|
//|     After calling this, all `pressed_to_wake()` and `woken_by_button()`
//|     calls will return False until the next boot.
//|     """
//|     ...
//|
static mp_obj_t badger2040_reset_pressed_to_wake(void) {
    board_clear_wake_button_state();
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(badger2040_reset_pressed_to_wake_obj, badger2040_reset_pressed_to_wake);

//| def wake_button_state() -> int:
//|     """Get the raw wake button state as a bitmask.
//|
//|     Each bit corresponds to a GPIO pin. For example, if button A (pin 12)
//|     was pressed, bit 12 will be set.
//|
//|     :return: Bitmask of button states captured at boot.
//|     """
//|     ...
//|
static mp_obj_t badger2040_wake_button_state(void) {
    return mp_obj_new_int(board_get_wake_button_state());
}
static MP_DEFINE_CONST_FUN_OBJ_0(badger2040_wake_button_state_obj, badger2040_wake_button_state);

//| def halt() -> None:
//|     """Halt the board to save power.
//|
//|     When running on battery, this turns off the 3.3V regulator, effectively
//|     powering down the board. The board will wake when any button is pressed.
//|
//|     When connected to USB, power cannot be cut, so this function simply
//|     returns. This is useful for debugging - your code continues to run
//|     when connected to a computer.
//|
//|     Typical usage pattern::
//|
//|         # Do your work
//|         update_display()
//|
//|         # Save state if needed
//|         save_app_state()
//|
//|         # Power down
//|         badger2040.halt()
//|
//|         # Code here only runs if on USB power
//|         while True:
//|             handle_button_presses()
//|     """
//|     ...
//|
static mp_obj_t badger2040_halt(void) {
    board_halt();
    return mp_const_none;
}
static MP_DEFINE_CONST_FUN_OBJ_0(badger2040_halt_obj, badger2040_halt);

static const mp_rom_map_elem_t badger2040_module_globals_table[] = {
    { MP_ROM_QSTR(MP_QSTR___name__), MP_ROM_QSTR(MP_QSTR_badger2040) },

    // Functions
    { MP_ROM_QSTR(MP_QSTR_woken_by_button), MP_ROM_PTR(&badger2040_woken_by_button_obj) },
    { MP_ROM_QSTR(MP_QSTR_pressed_to_wake), MP_ROM_PTR(&badger2040_pressed_to_wake_obj) },
    { MP_ROM_QSTR(MP_QSTR_pressed_to_wake_get_once), MP_ROM_PTR(&badger2040_pressed_to_wake_get_once_obj) },
    { MP_ROM_QSTR(MP_QSTR_reset_pressed_to_wake), MP_ROM_PTR(&badger2040_reset_pressed_to_wake_obj) },
    { MP_ROM_QSTR(MP_QSTR_wake_button_state), MP_ROM_PTR(&badger2040_wake_button_state_obj) },
    { MP_ROM_QSTR(MP_QSTR_halt), MP_ROM_PTR(&badger2040_halt_obj) },

    // Button constants
    { MP_ROM_QSTR(MP_QSTR_BUTTON_A), MP_ROM_INT(BADGER_BUTTON_A) },
    { MP_ROM_QSTR(MP_QSTR_BUTTON_B), MP_ROM_INT(BADGER_BUTTON_B) },
    { MP_ROM_QSTR(MP_QSTR_BUTTON_C), MP_ROM_INT(BADGER_BUTTON_C) },
    { MP_ROM_QSTR(MP_QSTR_BUTTON_UP), MP_ROM_INT(BADGER_BUTTON_UP) },
    { MP_ROM_QSTR(MP_QSTR_BUTTON_DOWN), MP_ROM_INT(BADGER_BUTTON_DOWN) },
    { MP_ROM_QSTR(MP_QSTR_BUTTON_USER), MP_ROM_INT(BADGER_BUTTON_USER) },
};
static MP_DEFINE_CONST_DICT(badger2040_module_globals, badger2040_module_globals_table);

const mp_obj_module_t badger2040_module = {
    .base = { &mp_type_module },
    .globals = (mp_obj_dict_t *)&badger2040_module_globals,
};

MP_REGISTER_MODULE(MP_QSTR_badger2040, badger2040_module);