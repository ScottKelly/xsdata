from pathlib import Path

from generated.models import TransSet
from xsdata.formats.dataclass.parsers import XmlParser

xml_file = "../altriareporting/tests/features/input_data/bg_testmart/condensed/2021-08-23.705_2_c.9.xml"
xml_string = Path(xml_file).read_text()
parser = XmlParser()
vp_transact_set: TransSet = parser.from_string(xml_string, TransSet)
print("done")