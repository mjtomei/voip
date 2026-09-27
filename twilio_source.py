"""Bundle the shared admission helper into each independently hosted Function."""
from pathlib import Path


def function_source(root, name):
    directory = Path(root) / "twilio"
    source = (directory / (name + ".js")).read_text()
    return ((directory / "access.js").read_text() + "\n" +
            (directory / "call-routing.js").read_text().replace("const { voiceAccess } = require('./access');\n", "") + "\n" +
            "const callRouting = exports.callRouting;\n" +
            source.replace("const { voiceAccess } = require('./access');\n", "").replace("const { callRouting } = require('./call-routing');\n", "")).encode()
