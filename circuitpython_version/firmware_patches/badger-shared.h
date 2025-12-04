// This file is part of the CircuitPython project: https://circuitpython.org
//
// SPDX-FileCopyrightText: Copyright (c) 2021 Scott Shawcroft for Adafruit Industries
//
// SPDX-License-Identifier: MIT
#pragma once

#include <stdint.h>
#include <stdbool.h>
#include "shared-bindings/digitalio/DigitalInOut.h"

extern digitalio_digitalinout_obj_t enable_pin_obj;

// Button pin definitions for Badger 2040
#define BADGER_BUTTON_DOWN  11
#define BADGER_BUTTON_A     12
#define BADGER_BUTTON_B     13
#define BADGER_BUTTON_C     14
#define BADGER_BUTTON_UP    15
#define BADGER_BUTTON_USER  23
#define BADGER_ENABLE_3V3   10

// Wake button state functions
// These capture button state at the earliest possible moment during boot,
// allowing detection of which button was pressed to wake the device.

// Get the raw wake button state bitmask
uint32_t board_get_wake_button_state(void);

// Check if any button was pressed to wake
bool board_woken_by_button(void);

// Check if a specific button was pressed to wake (non-destructive read)
bool board_button_pressed_to_wake(uint8_t pin);

// Check if a specific button was pressed to wake, then clear that button's state
// Use this for "get once" behavior to avoid re-triggering on the same press
bool board_button_wake_get_once(uint8_t pin);

// Clear all wake button state
void board_clear_wake_button_state(void);

// Halt the board by turning off the 3V3 regulator
// On battery: powers off the board
// On USB: continues running (useful for debugging)
void board_halt(void);