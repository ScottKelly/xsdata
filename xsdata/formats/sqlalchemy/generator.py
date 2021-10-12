from pathlib import Path
from typing import List, Optional, Iterator

from jinja2 import FileSystemLoader, Environment
from xsdata.utils.package import package_path

from xsdata.formats.mixins import GeneratorResult

from xsdata.formats.sqlalchemy.filters import SqlAlchemyTemplateFilters
from xsdata.models.config import GeneratorConfig

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

    def render(self, classes: List[Class]) -> Iterator[GeneratorResult]:
        self.filters.set_classes(classes)
        # render sqlalchemy registry for this package
        package_name = classes[0].package
        registry_path = package_path(classes[0].target_module).joinpath("registry").with_suffix(".py")
        yield GeneratorResult(path=registry_path,
                              title="registry",
                              source=self.render_sqlalchemy_registry(package_name))

        yield from super().render(classes)



    def render_classes(
        self, classes: List[Class], module_namespace: Optional[str]
    ) -> str:
        return super().render_classes(classes, module_namespace)

    def render_sqlalchemy_registry(self, package: str) -> str:
        template = self.env.get_template("registry.jinja2")

        # use top-level name of package
        schema_name = package.split(".")[0]

        return template.render(
            schema_name=schema_name,
        ).strip()

    @classmethod
    def init_filters(cls, config: GeneratorConfig) -> SqlAlchemyTemplateFilters:
        return SqlAlchemyTemplateFilters(config)
