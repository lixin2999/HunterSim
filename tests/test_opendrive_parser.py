"""OpenDRIVE 地图解析器单元测试（PROMPT-TEST-001）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from hunter_sim.common.exceptions import ConfigurationError
from hunter_sim.engine.opendrive_parser import OpenDriveParser

_VALID_XODR = """<?xml version="1.0"?>
<OpenDRIVE>
  <header revHeader="1.6" name="TownTest">
    <geoReference><![CDATA[[+proj=tmerc]]]></geoReference>
  </header>
  <road id="1" name="R1" length="100.0" junction="-1">
    <lanes>
      <laneSection id="0">
        <lane id="-1" type="driving"/>
        <lane id="-2" type="sidewalk"/>
      </laneSection>
    </lanes>
    <signals>
      <signal id="s1" s="10.0" t="2.0" type="1000001" country="OpenDRIVE"/>
    </signals>
  </road>
  <road id="2" name="R2" length="50.0" junction="3">
    <lanes>
      <laneSection id="0">
        <lane id="-1" type="driving"/>
      </laneSection>
    </lanes>
  </road>
  <junction id="3" name="J1">
    <connection id="0" incomingRoad="1" connectingRoad="2"/>
  </junction>
</OpenDRIVE>
"""


def _write(tmp_path: Path, content: str, name: str = "map.xodr") -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


class TestInit:
    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError):
            OpenDriveParser(tmp_path / "nope.xodr")


class TestParseValid:
    def test_summary_counts(self, tmp_path: Path) -> None:
        parser = OpenDriveParser(_write(tmp_path, _VALID_XODR))
        s = parser.parse()
        assert s.valid is True
        assert s.header_version == "1.6"
        assert s.map_name == "TownTest"
        assert s.total_road_count == 2
        assert s.total_junction_count == 1
        assert s.total_signal_count == 1
        assert s.total_length_m == pytest.approx(150.0)

    def test_road_lanes(self, tmp_path: Path) -> None:
        s = OpenDriveParser(_write(tmp_path, _VALID_XODR)).parse()
        r1 = next(r for r in s.roads if r.road_id == 1)
        assert len(r1.lanes) == 2
        assert {lane.lane_type for lane in r1.lanes} == {"driving", "sidewalk"}

    def test_signal_fields(self, tmp_path: Path) -> None:
        s = OpenDriveParser(_write(tmp_path, _VALID_XODR)).parse()
        sig = s.signals[0]
        assert sig.signal_id == "s1"
        assert sig.x == 10.0
        assert sig.y == 2.0

    def test_junction_connections(self, tmp_path: Path) -> None:
        s = OpenDriveParser(_write(tmp_path, _VALID_XODR)).parse()
        j = s.junctions[0]
        assert j.junction_id == 3
        assert 1 in j.connecting_road_ids


class TestParseErrors:
    def test_bad_xml(self, tmp_path: Path) -> None:
        p = _write(tmp_path, "<OpenDRIVE><unclosed>", name="bad.xodr")
        s = OpenDriveParser(p).parse()
        assert s.valid is False
        assert any("parse error" in e.lower() for e in s.errors)

    def test_wrong_root(self, tmp_path: Path) -> None:
        p = _write(tmp_path, "<NotOpenDRIVE/>", name="wrong.xodr")
        s = OpenDriveParser(p).parse()
        assert s.valid is False
        assert any("Root element" in e for e in s.errors)

    def test_unsupported_version(self, tmp_path: Path) -> None:
        content = _VALID_XODR.replace('revHeader="1.6"', 'revHeader="9.9"')
        s = OpenDriveParser(_write(tmp_path, content)).parse()
        assert s.valid is False
        assert any("version" in e.lower() for e in s.errors)

    def test_no_roads(self, tmp_path: Path) -> None:
        content = '<?xml version="1.0"?><OpenDRIVE><header revHeader="1.6"/></OpenDRIVE>'
        s = OpenDriveParser(_write(tmp_path, content)).parse()
        assert s.valid is False
        assert any("no roads" in e for e in s.errors)
