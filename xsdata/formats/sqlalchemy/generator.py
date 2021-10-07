from pathlib import Path
from typing import List, Optional

from jinja2 import FileSystemLoader, Environment

from xsdata.formats.dataclass.filters import Filters

from xsdata.formats.sqlalchemy.filters import SqlAlchemyTemplateFilters
from xsdata.models.config import GeneratorConfig
import inspect

from xsdata.codegen.models import Class

from xsdata.formats.dataclass.generator import DataclassGenerator


class SqlAlchemyDataClassGenerator(DataclassGenerator):
    def __init__(self, config: GeneratorConfig):
        """Override generator constructor to set templates directory and
        environment filters."""

        super().__init__(config)

        # combine search path with dataclass search path
        # so we can use dataclass templates as a fallback
        tpl_dir = Path(__file__).parent.joinpath("templates")
        self.env = Environment(loader=FileSystemLoader([str(tpl_dir)] + self.env.loader.searchpath), autoescape=False)

        self.filters = self.init_filters(config)
        self.filters.register(self.env)

    def render_classes(
        self, classes: List[Class], module_namespace: Optional[str]
    ) -> str:

        self.filters.set_classes(classes)
        return super().render_classes(classes, module_namespace)

    @classmethod
    def init_filters(cls, config: GeneratorConfig) -> SqlAlchemyTemplateFilters:
        return SqlAlchemyTemplateFilters(config)
