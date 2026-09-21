"""Built-in GPIO skill: drive a GPIO pin's output level (on/off/toggle).

The skill is pin-agnostic; the caller passes the GPIO number. GPIO38 happens to
have an on-board LED attached, so "turn on the LED on gpio38" maps to
``pin=38, action=on``.
"""


def run(args):
    import machine

    pin_no = args.get("pin")
    if pin_no is None:
        return "missing required arg: pin"
    action = args.get("action", "toggle")
    pin = machine.Pin(pin_no, machine.Pin.OUT)
    if action == "on":
        pin.value(1)
    elif action == "off":
        pin.value(0)
    elif action == "toggle":
        cur = pin.value()
        pin.value(0 if cur else 1)
    else:
        return "unknown action: " + str(action)
    return "GPIO" + str(pin_no) + " " + action
