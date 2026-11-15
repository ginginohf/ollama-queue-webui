# Visual examples

[Русский](EXAMPLES.ru.md) | [Project overview](../README.md)

The shipped chat interface with synthetic user, model list and conversation API responses. This is not a real model response or inference benchmark.

![Demo interface](images/overview.png)

## Reproduce the screenshots

Use a separate tooling environment. The capture script intercepts requests and blocks external hosts; no production server is needed. It renders the actual shipped HTML/CSS/JavaScript rather than a manually drawn mockup.

```sh
python3 -m venv /tmp/ui-capture-venv
. /tmp/ui-capture-venv/bin/activate
pip install playwright
playwright install chromium
python3 examples/capture_ui.py
```

Images are written to `docs/images/`. Alternatively set `DEMO_CHROME` to an existing Chrome executable. Interface labels currently remain Russian; documentation language does not change application localization.
