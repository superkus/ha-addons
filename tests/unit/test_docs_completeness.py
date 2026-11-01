"""Documentation completeness: the plugin authoring guide has a section per contract item (task 16.2).

Requirement 10.1 has the addon provide developer documentation that describes each element of the
Plugin_Interface, the failure classifications, and every field of the Power_Reading contract.
``docs/writing-a-plugin.md`` is that document, and nothing but this module keeps it in step with the
contract it describes.

Every expectation is derived from :mod:`remote_energy_meter.plugins.interface` rather than restated here.
That is the whole point: adding a classification to :class:`FailureClassification`, a field to
:class:`PowerReading`, or a name to :data:`PLUGIN_INTERFACE_ELEMENTS` fails this module until the guide
documents it. A list of item names copied into this file would go stale on exactly the change the test
exists to catch.

## Why headings, and not ``in``

A bare substring search over the document would pass on an incidental mention in prose -- and every one of
these names *is* mentioned in prose, repeatedly, in the overview and the checklist. So the search would
pass for an item nobody wrote a section for, which is the one false pass this module must not give. The
document is therefore reduced to its ``##``-to-``####`` headings, and an item counts as documented only
when a heading names it. Backticks around the name are accepted, since Markdown code spans are the
document's own convention for an identifier in a heading.

What this module does *not* check is the prose: not its length, not its ordering, not whether it says
anything useful. A section that exists and is wrong is a review problem. A section that does not exist is
a drift problem, and that is the one a test can catch.
"""

from __future__ import annotations

import re
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Final

from remote_energy_meter.plugins.interface import (
    PLUGIN_INTERFACE_ELEMENTS,
    FailureClassification,
    PowerReading,
)

#: The developer documentation Requirement 10.1 calls for.
PLUGIN_GUIDE: Final = Path(__file__).resolve().parents[2] / "docs" / "writing-a-plugin.md"

#: A section heading, two to four hashes deep. The ``#`` level is not part of what is asserted -- the
#: document nests items under topic headings, and a future reshuffle of the levels is not drift.
_HEADING_RE: Final = re.compile(r"^#{2,4}\s+(?P<title>.+?)\s*$", re.MULTILINE)


def heading_titles(document: str) -> tuple[str, ...]:
    """The heading texts of ``document``, in order, with Markdown code spans unwrapped.

    Backticks are stripped rather than matched around, so ``### plugin_id`` and ``### `plugin_id``` are
    the same heading as far as this module is concerned.
    """
    return tuple(match["title"].replace("`", "").strip() for match in _HEADING_RE.finditer(document))


def documented_names(document: str) -> frozenset[str]:
    """Every whitespace-separated word appearing in a heading of ``document``.

    Split into words so a heading that carries more than the bare name -- ``### watts (instantaneous
    power)`` -- still counts as that item's section, while a name buried in body prose does not.
    """
    return frozenset(word.strip(":,.()") for title in heading_titles(document) for word in title.split())


#: The Power_Reading field names, derived from the dataclass rather than listed.
POWER_READING_FIELDS: Final[tuple[str, ...]] = tuple(field.name for field in dataclass_fields(PowerReading))

#: The classification names as the document spells them: the enum *values*, which are the strings the
#: logs and the guide both use, not the Python member names.
FAILURE_CLASSIFICATIONS: Final[tuple[str, ...]] = tuple(item.value for item in FailureClassification)


def test_the_plugin_guide_exists_and_is_not_empty() -> None:
    """Checked first and alone, so a missing document reports as one problem and not as fourteen."""
    assert PLUGIN_GUIDE.is_file(), (
        f"Requirement 10.1 calls for developer documentation of the Plugin_Interface, and {PLUGIN_GUIDE} "
        "is where the rest of this module looks for it"
    )
    assert PLUGIN_GUIDE.read_text(encoding="utf-8").strip(), f"{PLUGIN_GUIDE} is empty"


def test_every_interface_element_has_a_section() -> None:
    """Requirement 10.1: each of the four Plugin_Interface elements is described in its own section."""
    documented = documented_names(PLUGIN_GUIDE.read_text(encoding="utf-8"))
    missing = [name for name in PLUGIN_INTERFACE_ELEMENTS if name not in documented]

    assert not missing, (
        f"{PLUGIN_GUIDE.name} has no section for the Plugin_Interface element(s) {missing}; a plugin "
        "author has nothing to read about what to provide or what it must return"
    )


def test_every_failure_classification_has_a_section() -> None:
    """Requirement 10.1: the failure classifications a plugin may return are each described.

    Derived from the enum, so a new classification is undocumented until someone writes its section --
    and an undocumented classification is one a plugin author will not know to return.
    """
    documented = documented_names(PLUGIN_GUIDE.read_text(encoding="utf-8"))
    missing = [name for name in FAILURE_CLASSIFICATIONS if name not in documented]

    assert not missing, (
        f"{PLUGIN_GUIDE.name} has no section for the failure classification(s) {missing}; they are values "
        "a plugin is expected to return, so leaving them undocumented leaves them unused or misused"
    )


def test_every_power_reading_field_has_a_section() -> None:
    """Requirement 10.1: every field of the Power_Reading contract is described.

    Derived from the dataclass fields, so adding a field to :class:`PowerReading` fails here until the
    guide says what a plugin must put in it.
    """
    documented = documented_names(PLUGIN_GUIDE.read_text(encoding="utf-8"))
    missing = [name for name in POWER_READING_FIELDS if name not in documented]

    assert not missing, (
        f"{PLUGIN_GUIDE.name} has no section for the Power_Reading field(s) {missing}; every field is "
        "required and has no default, so a plugin author must be told what belongs in it"
    )
