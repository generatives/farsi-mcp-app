import shutil

import pytest
import yaml

from app import curriculum


def test_reduced_curriculum_loads(cur):
    assert len(cur.grammar) == 5 and len(cur.functions) == 5
    assert all(g.stage == 0 for g in cur.grammar.values())
    assert cur.functions["f.thank"].phrases[-1] == "p.dastet-dard-nakone"


def _copy(tmp_path):
    shutil.copytree("curriculum", tmp_path / "c")
    return tmp_path / "c"


def _edit(path, fn):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    fn(data)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


@pytest.mark.parametrize(
    "file,change,message",
    [
        ("phrases.yaml", lambda d: d[0].update(tags=["dancing"]), "not in the topic list"),
        ("functions.yaml", lambda d: d[0]["phrases"].append("p.nope"), "unknown set phrase"),
        ("grammar.yaml", lambda d: d[1].update(id="g.to-be"), "duplicate id"),
        ("words/tier1.yaml", lambda d: d[0].update(pos="verbish"), r"w\.man\.pos: Input should be 'noun'"),
        ("grammar.yaml", lambda d: d[0].update(stage="zero"), r"g\.to-be\.stage: Input should be a valid integer"),
        ("phrases.yaml", lambda d: d[0].update(colour="red"), r"p\.salam\.colour: Extra inputs are not permitted"),
        ("phrases.yaml", lambda d: d[0].pop("en"), r"p\.salam\.en: Field required"),
        ("words/tier1.yaml", lambda d: d[0].update(id="x.man"), "must start with w."),
    ],
)
def test_validation_refuses_broken_files(tmp_path, file, change, message):
    d = _copy(tmp_path)
    _edit(d / file, change)
    with pytest.raises(curriculum.CurriculumError, match=message):
        curriculum.load(str(d))


def test_curriculum_text_has_no_transliteration(cur):
    """Claude reads this text and copies its style, so Persian in it must be in Persian script."""
    for table in (cur.grammar, cur.functions, cur.phrases, cur.words):
        for item in table.values():
            for value in item.model_dump().values():
                assert "â" not in str(value), f"{item.id}: transliterated Persian in {value!r}"
