from __future__ import annotations

import pytest
from conftest import raw_env

from oo.envs import Env
from oo.projects import ChoiceError, EnvClass, Project, pick_class, pick_project

TOKYO = EnvClass(id="c-tk", name="m7i.xlarge", runner="Tokyo")
FRANKFURT = EnvClass(id="c-fr", name="m7i.xlarge", runner="Frankfurt")
FRANKFURT_SMALL = EnvClass(id="c-fr-s", name="t4g.small", runner="Frankfurt")
SYDNEY = EnvClass(id="c-sy", name="c7g.large", runner="Sydney")
CLASSES = [TOKYO, FRANKFURT, FRANKFURT_SMALL, SYDNEY]

WIDGETS = Project(id="p-1", name="Widgets", class_ids=("c-tk", "c-fr", "c-sy"))
GADGETS = Project(id="p-2", name="Gadgets", class_ids=("c-sy",))
PROJECTS = [WIDGETS, GADGETS]


def env(id: str, created: str, project: str = "p-1", env_class: str = "c-fr") -> Env:
    return Env.from_json(raw_env(id, id, created=created, project=project, env_class=env_class))


ENVS = [
    env("old", "2026-09-01T00:00:00Z", project="p-2", env_class="c-sy"),
    env("new", "2026-09-20T00:00:00Z"),
]


def test_project_parses_offered_classes_in_ui_order():
    project = Project.from_json(
        {
            "id": "p",
            "metadata": {"name": "Widgets"},
            "environmentClasses": [
                {"environmentClassId": "b", "order": 1},
                {"environmentClassId": "a"},
            ],
        }
    )
    assert project.class_ids == ("a", "b")


def test_project_defaults_to_your_newest_environments():
    assert pick_project(PROJECTS, ENVS, None) is WIDGETS


@pytest.mark.parametrize("wanted", ["p-2", "gadgets", "Gadgets"])
def test_project_by_id_or_name(wanted):
    assert pick_project(PROJECTS, ENVS, wanted) is GADGETS


def test_project_needs_something_to_go_on():
    with pytest.raises(ChoiceError):
        pick_project(PROJECTS, [], None)
    with pytest.raises(ChoiceError):
        pick_project(PROJECTS, ENVS, "nope")


def test_class_defaults_to_what_your_newest_environment_on_the_project_uses():
    assert pick_class(CLASSES, WIDGETS, ENVS, None) is FRANKFURT
    assert pick_class(CLASSES, GADGETS, ENVS, None) is SYDNEY


def test_class_falls_back_to_the_projects_first_choice():
    assert pick_class(CLASSES, WIDGETS, [], None) is TOKYO
    assert pick_class(CLASSES, Project(id="p-3", name="Bare"), [], None) is None


@pytest.mark.parametrize(
    ("wanted", "expected"),
    [
        ("c-sy", SYDNEY),
        ("frankfurt", FRANKFURT),  # the project only offers the big Frankfurt machine
        ("TOKYO", TOKYO),
        ("frankfurt/t4g", FRANKFURT_SMALL),  # but you can ask for any class by name
        ("sydney c7g", SYDNEY),
    ],
)
def test_class_by_id_or_words(wanted, expected):
    assert pick_class(CLASSES, WIDGETS, ENVS, wanted) is expected


def test_class_complains_when_unclear():
    with pytest.raises(ChoiceError, match="could mean"):
        pick_class(CLASSES, WIDGETS, ENVS, "m7i")
    with pytest.raises(ChoiceError, match="No environment class"):
        pick_class(CLASSES, WIDGETS, ENVS, "mars")
