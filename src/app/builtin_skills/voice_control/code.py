"""Built-in voice session skill: end the conversation or adjust speaker volume.

action=exit: the LLM passes the farewell to speak as the "ack" argument.
The bus extracts it into the reply metadata (see Bus._finalize and
config.VOICE_CONTROL_SKILL); the voice channel speaks it and returns to
the wake stage.

action=volume: reads/writes the persisted speaker volume (agent config
key "volume", 0-100) via the injected get_volume/set_volume helpers.
The voice channel re-reads the value before every playback, so the new
volume applies immediately — including to the spoken confirmation.
"""


def run(args):
    action = args.get("action")
    if action is None:
        # Backward compatibility with the original exit-only schema.
        action = "exit" if args.get("ack") else "volume"
    if action == "exit":
        ack = args.get("ack") or ""
        return (
            "Voice conversation ending; your farewell '{}' will be spoken to the user. "
            "Your text reply will NOT be spoken, so keep it minimal.".format(ack)
        )
    if action != "volume":
        return "unknown action: " + str(action)

    current = get_volume()
    level = args.get("level")
    adjust = args.get("adjust")
    if level is None and adjust is None:
        return "current volume: {}".format(current)
    if level is not None:
        target = level
    else:
        try:
            target = current + int(adjust)
        except (TypeError, ValueError):
            return "invalid adjust: {}".format(adjust)
    try:
        target = int(target)
    except (TypeError, ValueError):
        return "invalid level: {}".format(level)
    new = set_volume(target)  # clamped to 0-100 and persisted by the agent
    return "volume changed: {} -> {}".format(current, new)
