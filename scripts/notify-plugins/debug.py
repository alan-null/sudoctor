"""Print the message as JSON to the scheduler log. Useful for testing rules. No options."""
import json


def send(message, options):
    print('notify debug: ' + json.dumps(message, sort_keys=True), flush=True)
