import math
import re
import textwrap
from typing import Any
from typing import Callable
from typing import Dict
from typing import Iterable
from typing import List
from typing import Optional
from typing import Tuple
from typing import Type
from xml.etree.ElementTree import QName
from xml.sax.saxutils import quoteattr

from docformatter import format_code
from jinja2 import Environment
from markupsafe import Markup

from xsdata.codegen.models import Attr, Extension
from xsdata.codegen.models import AttrType
from xsdata.codegen.models import Class
from xsdata.formats.converter import converter
from xsdata.models.config import DocstringStyle
from xsdata.models.config import GeneratorAlias
from xsdata.models.config import GeneratorConfig
from xsdata.models.config import OutputFormat
from xsdata.utils import collections
from xsdata.utils import namespaces
from xsdata.utils import text


def index_aliases(aliases: List[GeneratorAlias]) -> Dict:
    return {alias.source: alias.target for alias in aliases}


class Filters:

    DEFAULT_KEY = "default"
    FACTORY_KEY = "default_factory"

    __slots__ = (
        "class_aliases",
        "field_aliases",
        "package_aliases",
        "module_aliases",
        "class_case",
        "field_case",
        "constant_case",
        "package_case",
        "module_case",
        "class_safe_prefix",
        "field_safe_prefix",
        "constant_safe_prefix",
        "package_safe_prefix",
        "module_safe_prefix",
        "docstring_style",
        "max_line_length",
        "relative_imports",
        "format",
        "import_patterns",
        "classes"
    )

    def __init__(self, config: GeneratorConfig):
        self.class_aliases: Dict = index_aliases(config.aliases.class_name)
        self.field_aliases: Dict = index_aliases(config.aliases.field_name)
        self.package_aliases: Dict = index_aliases(config.aliases.package_name)
        self.module_aliases: Dict = index_aliases(config.aliases.module_name)
        self.class_case: Callable = config.conventions.class_name.case
        self.field_case: Callable = config.conventions.field_name.case
        self.constant_case: Callable = config.conventions.constant_name.case
        self.package_case: Callable = config.conventions.package_name.case
        self.module_case: Callable = config.conventions.module_name.case
        self.class_safe_prefix: str = config.conventions.class_name.safe_prefix
        self.field_safe_prefix: str = config.conventions.field_name.safe_prefix
        self.constant_safe_prefix: str = config.conventions.constant_name.safe_prefix
        self.package_safe_prefix: str = config.conventions.package_name.safe_prefix
        self.module_safe_prefix: str = config.conventions.module_name.safe_prefix
        self.docstring_style: DocstringStyle = config.output.docstring_style
        self.max_line_length: int = config.output.max_line_length
        self.relative_imports: bool = config.output.relative_imports
        self.format = config.output.format

        # Build things
        self.import_patterns = self.build_import_patterns()

    def set_classes(self, classes: List[Class]) -> None:
        self.classes = classes

    def register(self, env: Environment):
        env.globals.update(
            {
                "docstring_name": self.docstring_style.name.lower(),
                "class_annotation": self.build_class_annotation(self.format),
            }
        )
        env.filters.update(
            {
                "field_name": self.field_name,
                "field_type": self.field_type,
                "field_default": self.field_default_value,
                "field_metadata": self.field_metadata,
                "field_definition": self.field_definition,
                "class_name": self.class_name,
                "class_params": self.class_params,
                "table_name": self.table_name,
                "format_string": self.format_string,
                "format_docstring": self.format_docstring,
                "constant_name": self.constant_name,
                "constant_value": self.constant_value,
                "default_imports": self.default_imports,
                "format_metadata": self.format_metadata,
                "type_name": self.type_name,
                "text_wrap": self.text_wrap,
                "clean_docstring": self.clean_docstring,
                "import_module": self.import_module,
                "import_class": self.import_class,
                "relationships": self.relationships,
                "non_relational": self.non_relational,
                "extension_attrs": self.extension_attrs
            }
        )

    @classmethod
    def build_class_annotation(cls, format: OutputFormat) -> str:
        args = []
        if not format.repr:
            args.append("repr=False")
        if not format.eq:
            args.append("eq=False")
        if format.order:
            args.append("order=True")
        if format.unsafe_hash:
            args.append("unsafe_hash=True")
        if format.frozen:
            args.append("frozen=True")
        if format.slots:
            args.append("slots=True")
        if format.kw_only:
            args.append("kw_only=True")

        return f"@dataclass({', '.join(args)})" if args else "@dataclass"

    def class_params(self, obj: Class):
        is_enum = obj.is_enumeration
        for attr in obj.attrs:
            name = attr.name
            docstring = self.clean_docstring(attr.help)
            if is_enum:
                yield self.constant_name(name, obj.name), docstring
            else:
                yield self.field_name(name, obj.name), docstring

    def class_name(self, name: str, parents=None) -> str:
        """Convert the given string to a class name according to the selected
        conventions or use an existing alias."""
        alias = self.class_aliases.get(name)
        if alias:
            return alias
        if parents:
            name = ""
            for parent in parents:
                name += self.class_name(parent)
        return self.safe_name(name, self.class_safe_prefix, self.class_case)


    def get_class_for_extension(self, extension: Extension) -> Class:
        if self.is_complex_type(self.type_name(extension.type)):
            return self.find_class_by_qname(extension.type.qname, [])


    def extension_attrs(self, extensions: List[Extension]) -> List[Attr]:
        attrs = []
        for extension in extensions:
            clazz = self.get_class_for_extension(extension)
            attrs += clazz.attrs

        return attrs

    def field_definition(
        self,
        attr: Attr,
        ns_map: Dict,
        parent_namespace: Optional[str],
        parents: List[str],
    ) -> str:
        """Return the field definition with any extra metadata."""
        #type_names = self.type_names(attr, parents)

        default_value = self.field_default_value(attr, ns_map)
        metadata = self.field_metadata(attr, parent_namespace, parents)

        kwargs: Dict[str, Any] = {}
        if attr.fixed:
            kwargs["init"] = False

        if default_value is not False:
            key = self.FACTORY_KEY if attr.is_factory else self.DEFAULT_KEY
            kwargs[key] = default_value

        if metadata:
            kwargs["metadata"] = metadata

        return f"field({self.format_arguments(kwargs, 4)})"

    def non_relational(self, attrs: List[Attr]):
        """
        Returns a list of attrs with all relational attrs removed.
        """
        non_relational_attrs: List[Attr] = []
        for attr in attrs:
            attr_types = self.type_names(attr, [])
            self.convert_primitive_types(attr_types)
            if not self.has_complex_types(attr_types):
                non_relational_attrs.append(attr)

        return non_relational_attrs

    def build_backwards_relationships(self, obj, classes: List[Class], relationships: List[str], parents: List[str]) -> List[str]:
        """
        Finds all other classes that have a relationship to obj recursively and
        adds a back_populating relationship field
        """
        for clazz in classes:
            if clazz == obj:
                continue
            for ref_attr in clazz.attrs:
                ref_attr_types = self.type_names(ref_attr, [])
                self.convert_primitive_types(ref_attr_types)
                if not self.has_complex_types(ref_attr_types):
                    continue

                ref_attr_class = self.find_class_by_qname(ref_attr.types[0].qname, [clazz.qname])
                obj_fqname = obj.fqname if obj.fqname else self.fqname(obj)
                ref_attr_fqname = ref_attr_class.fqname
                if ref_attr_fqname == obj_fqname:
                    if ref_attr.is_list:
                        full_class_name = clazz.fqname if clazz.fqname else self.fqname(clazz)
                        table_name = self.table_name(full_class_name.split("."))
                        relationships.append('{}_id: int = field(default=None, metadata={{"sa": Column(ForeignKey(\"{}.id\", use_alter=True))}})'.format(self.field_case(clazz.qname), table_name))
                        relationships.append("{qname}: Optional[\"{fqname}\"] = field(default=None, metadata={{\"sa\": relationship(\"{fqname}\", foreign_keys=[{qname}_id.metadata[\"sa\"]], back_populates=\"{attr_name}\")}})".format(
                            qname=self.field_case(clazz.qname),
                            fqname=self.class_name(full_class_name),
                            attr_name=self.field_case(ref_attr.name)
                        ))
                    else:
                        pass
                        # full_class_name = self.fqname(clazz)
                        #table_name = self.table_name(full_class_name.split("."))
                        #relationships.append("{}_id: int = Field(foreign_key=\"{}.id\")".format(self.field_case(clazz.qname), table_name))
                        # relationships.append("{qname}: \"{fqname}\" = relationship(\"{fqname}\", back_populates=\"{attr_name}\")".format(
                        #     qname=self.field_case(clazz.qname),
                        #     fqname=self.class_name(full_class_name),
                        #     attr_name=self.field_case(ref_attr.name)
                        # ))
            # TODO this might not be required if the class_list has all of the classes in it
            self.build_backwards_relationships(obj, clazz.inner, relationships, parents)
        # find all classes that have a list reference to it
        return relationships

    def build_relationships(self, obj: Class, relationships: List[str], parents: List[str]) -> None:
        for attr in obj.attrs:
            attr_types = self.type_names(attr, [])
            self.convert_primitive_types(attr_types)
            if not self.has_complex_types(attr_types):
                continue
            attr_class = self.find_class_by_qname(attr.types[0].qname, parents)
            if attr.is_list or attr_class.is_enumeration or attr.is_enumeration:
                # list attributes are already handled by the normal field population
                continue
            else:
                full_class_name = attr_class.fqname if attr_class.fqname else self.fqname(attr_class)
                table_name = self.table_name(full_class_name.split("."))
                relationships.append(
                    '{}_id: int = field(default=None, metadata={{"sa": Column(ForeignKey(\"{}.id\", use_alter=True))}})'.format(
                        self.field_case(attr.name), table_name))
                # relationships.append(
                #     "{name}: \"{fqname}\" = relationship(\"{fqname}\", back_populates=\"{attr_name}\")".format(
                #         name=self.field_case(attr.name),
                #         fqname=self.class_name(full_class_name),
                #         attr_name=self.field_case(obj.qname)
                #     ))


    def relationships(self,
        obj: Class,
        parents: List[str]
    ) -> List[str]:
        relationships = []
        self.build_relationships(obj, relationships, parents)
        self.build_backwards_relationships(obj, self.classes, relationships, parents)
        # find all classes that have a list reference to it
        return relationships

    def _fqname_recursive(self, obj: Class, class_list: List[Class]) -> str:
        for clazz in class_list:
            if clazz.qname == obj.qname:
                return obj.qname
            elif clazz.inner:
                inner_clazz_result = self._fqname_recursive(obj, clazz.inner)
                if inner_clazz_result:
                    return clazz.qname + "." + inner_clazz_result

    _fqname_map: Dict[int, str] = {}

    def fqname(self, obj: Class) -> str:
        obj_id = id(obj)
        if obj_id not in self._fqname_map:
            self._fqname_map[obj_id] = self._fqname_recursive(obj, self.classes)
        return self._fqname_map[obj_id]



    def _compare_class(self, hierachy: str, clazz: Class, qname: str) -> Class:
        if hierachy:
            hierachy += "." + self.class_name(clazz.qname)
        else:
            hierachy = self.class_name(clazz.qname)
        if hierachy.split(".")[-1] == self.class_name(qname):
            clazz.fqname = hierachy
            return clazz
        for inner_clazz in clazz.inner:
            match = self._compare_class(hierachy, inner_clazz, qname)
            if match:
                return match
            else:
                self._compare_class(hierachy, inner_clazz, qname.split(".")[-1])
                if match:
                    return match

    def find_class_by_qname(self, qname: str, parents: List[str]) -> Class:
        # first find the class for the parents and prefer to grab the class from a
        # defined inner class
        for clazz in self.classes:
            if clazz.qname in parents:
                match = self._compare_class("", clazz, qname)
                if match:
                    return match
        # else, try to find any matching class for the attr
        for clazz in self.classes:
            match = self._compare_class("", clazz, qname)
            if match:
                return match
        raise ValueError(f"Can't find class for qname {qname}")

    def table_name(self, parents: List[str]):
        """
        Returns SQL table name for a class
        """
        parents = [self.class_name(x) for x in parents]

        # do double underscore to avoid situations where Alembic
        # may generate conflicting index names
        table_name = "__".join([self.field_case(parent) for parent in parents])

        # max identifier in postgres is 63 characters
        while len(table_name) > 63:
            table_name = table_name.split("__", 1)[-1]

        return table_name

    def field_name(self, name: str, class_name: str) -> str:
        """
        Convert the given name to a field name according to the selected
        conventions or use an existing alias.

        Provide the class name as context for the naming schemes.
        """
        alias = self.field_aliases.get(name)
        if alias:
            return alias

        return self.safe_name(
            name, self.field_safe_prefix, self.field_case, class_name=class_name
        )

    def constant_name(self, name: str, class_name: str) -> str:
        """
        Convert the given name to a constant name according to the selected
        conventions or use an existing alias.

        Provide the class name as context for the naming schemes.
        """
        alias = self.field_aliases.get(name)
        if alias:
            return alias

        return self.safe_name(
            name, self.constant_safe_prefix, self.constant_case, class_name=class_name
        )

    def module_name(self, name: str) -> str:
        """Convert the given string to a module name according to the selected
        conventions or use an existing alias."""
        alias = self.module_aliases.get(name)
        if alias:
            return alias

        return self.safe_name(
            namespaces.clean_uri(name), self.module_safe_prefix, self.module_case
        )

    def package_name(self, name: str) -> str:
        """Convert the given string to a package name according to the selected
        conventions or use an existing alias."""

        alias = self.package_aliases.get(name)
        if alias:
            return alias

        if not name:
            return name

        return ".".join(
            self.package_aliases.get(part)
            or self.safe_name(part, self.package_safe_prefix, self.package_case)
            for part in name.split(".")
        )

    def type_name(self, attr_type: AttrType) -> str:
        """Return native python type name or apply class name conventions."""
        datatype = attr_type.datatype
        if datatype:
            return datatype.type.__name__

        return self.class_name(attr_type.alias or attr_type.name)

    def safe_name(
        self, name: str, prefix: str, name_case: Callable, **kwargs: Any
    ) -> str:
        """Sanitize names for safe generation."""
        if not name:
            return self.safe_name(prefix, prefix, name_case, **kwargs)

        if re.match(r"^-\d*\.?\d+$", name):
            return self.safe_name(f"{prefix}_minus_{name}", prefix, name_case, **kwargs)

        slug = text.alnum(name)
        if not slug or not slug[0].isalpha():
            return self.safe_name(f"{prefix}_{name}", prefix, name_case, **kwargs)

        result = name_case(name, **kwargs)
        if text.is_reserved(result):
            return self.safe_name(f"{name}_{prefix}", prefix, name_case, **kwargs)

        return result

    def import_module(self, module: str, from_module: str) -> str:
        """Convert import module to relative path if config is enabled."""
        if self.relative_imports:
            mp = module.split(".")
            fp = from_module.split(".")
            index = 0

            # Find common parts index
            while len(mp) > index and len(fp) > index and mp[index] == fp[index]:
                index += 1

            if index > 0:
                # Replace common parts with dots
                return f"{'.' * max(1, len(fp) - index)}{'.'.join(mp[index:])}"

        return module

    def import_class(self, name: str, alias: Optional[str]) -> str:
        """Convert import class name with alias support."""
        if alias:
            return f"{self.class_name(name)} as {self.class_name(alias)}"

        return self.class_name(name)

    def field_metadata(
        self, attr: Attr, parent_namespace: Optional[str], parents: List[str]
    ) -> Dict:
        """Return a metadata dictionary for the given attribute."""

        name = namespace = None

        if not attr.is_nameless and attr.local_name != self.field_name(
            attr.name, parents[-1]
        ):
            name = attr.local_name

        if parent_namespace != attr.namespace or attr.is_attribute:
            namespace = attr.namespace

        restrictions = attr.restrictions.asdict(attr.native_types)

        metadata = {
            "name": name,
            "type": attr.xml_type,
            "namespace": namespace,
            "mixed": attr.mixed,
            "choices": self.field_choices(attr, parent_namespace, parents),
            "sa": self.sql_alchemy_column(name, attr, parent_namespace, parents),
            **restrictions,
        }

        if self.docstring_style == DocstringStyle.ACCESSIBLE and attr.help:
            metadata["doc"] = self.clean_docstring(attr.help, False)

        return self.filter_metadata(metadata)

    def sql_alchemy_column(self, name: str, attr: Attr, parent_namespace: Optional[str], parents: List[str]) -> str:
        # TODO attr.restrictions
        column_fmt = "Column({})"
        type_names = self.type_names(attr, parents)
        self.convert_primitive_types(type_names)
        type_name = type_names[0]

        if len(type_names) > 1:
            raise ValueError("Multiple types for foreign key is unsupported")
        if type_name in ("dict", "object"):
            return Markup(column_fmt.format("JSONB"))
        elif type_name == "bytes":
            return Markup(column_fmt.format("LargeBinary()"))
        elif type_name == "datetime":
            return Markup(column_fmt.format("DateTime"))
        elif type_name == "bool":
            return Markup(column_fmt.format("Boolean"))
        elif type_name == "int":
            return Markup(column_fmt.format("Integer"))
        elif type_name == "Decimal":
            return Markup(column_fmt.format("Numeric"))
        elif type_name == "str":
            return Markup(column_fmt.format("String"))
        elif self.has_complex_types(type_names):
            attr_class = self.find_class_by_qname(attr.types[0].qname, parents)
            if attr_class.is_enumeration:
                return Markup(column_fmt.format(f"SqlEnum({type_name}, name=\"{'_'.join([type_name]+parents)}\")"))
            else:
                if attr.is_tokens:
                    raise ValueError("No idea how to handle lists of lists")

                if attr.is_list:
                    if name == "trKioskOrder":
                        # strange special case where these two models have multiple foreign keys
                        # going to each other and SQL alchemy can't automatically
                        # figure out the primary join
                        return Markup(f"relationship({self.class_name(type_name)}, primaryjoin='TrHeaderType.id==TrTickNum.tr_header_type_id')")
                    elif name == "trRecall":
                        return Markup(f"relationship({self.class_name(type_name)}, primaryjoin=\"TrRecall.tr_header_type_id==TrHeaderType.id\")")
                    else:
                        return Markup(f"relationship({self.class_name(type_name)})")
                    # return "relationship(back_populates=\"{}\")".format(
                    #     self.field_case(obj.qname))
                else:
                    # handled in the relationships function
                    return Markup(f"relationship({self.class_name(type_name)}, foreign_keys=[{self.field_name(attr.name, parent_namespace)}_id.metadata[\"sa\"]])")
                    # if attr_class.qname not in type_names[0]:
                    #     table_name = self.table_name(type_names[0].split("."))
                    # else:
                    #     table_name = self.table_name(
                    #         [item for t in type_names for item in t.split(".")])
                    # return Markup(column_fmt.format(f"ForeignKey(\"{table_name}.id\")"))
        else:
            raise ValueError(f"Could not find matching SQL Type for XML Type(s): {type_names}")
    def field_choices(
        self, attr: Attr, parent_namespace: Optional[str], parents: List[str]
    ) -> Optional[Tuple]:
        """
        Return a list of metadata dictionaries for the choices of the given
        attribute.

        Return None if attribute has no choices.
        """

        if not attr.choices:
            return None

        result = []
        for choice in attr.choices:

            types = choice.native_types
            restrictions = choice.restrictions.asdict(types)
            namespace = (
                choice.namespace if parent_namespace != choice.namespace else None
            )

            metadata = {
                "name": choice.name,
                "wildcard": choice.is_wildcard,
                "type": self.choice_type(choice, parents),
                "namespace": namespace,
            }

            if choice.is_nameless:
                del metadata["name"]

            default_key = self.FACTORY_KEY if choice.is_factory else self.DEFAULT_KEY
            metadata[default_key] = self.field_default_value(choice)
            metadata.update(restrictions)

            if self.docstring_style == DocstringStyle.ACCESSIBLE and choice.help:
                metadata["doc"] = self.clean_docstring(choice.help, False)

            result.append(self.filter_metadata(metadata))

        return tuple(result)

    @classmethod
    def filter_metadata(cls, data: Dict) -> Dict:
        return {
            key: value
            for key, value in data.items()
            if value is not None and value is not False
        }

    def format_arguments(self, data: Dict, indent: int = 0) -> str:
        """Return a pretty keyword arguments representation."""
        ind = " " * indent
        fmt = "    {}{}={}"
        lines = [
            fmt.format(ind, key, self.format_metadata(value, indent + 4, key))
            for key, value in data.items()
        ]

        return "\n{}\n{}".format(",\n".join(lines), ind) if lines else ""

    def format_metadata(self, data: Any, indent: int = 0, key: str = "") -> str:
        """Prettify field metadata for code generation."""

        if isinstance(data, dict):
            return self.format_dict(data, indent)

        if collections.is_array(data):
            return self.format_iterable(data, indent)

        if isinstance(data, str):
            return self.format_string(data, indent, key, 4)

        return self.literal_value(data)

    def format_dict(self, data: Dict, indent: int) -> str:
        """Return a pretty string representation of a dict."""
        ind = " " * indent
        fmt = '    {}"{}": {},'
        lines = [
            fmt.format(ind, key, self.format_metadata(value, indent + 4, key))
            for key, value in data.items()
        ]

        return "{{\n{}\n{}}}".format("\n".join(lines), ind)

    def format_iterable(self, data: Iterable, indent: int) -> str:
        """Return a pretty string representation of an iterable."""
        ind = " " * indent
        fmt = "    {}{},"
        lines = [
            fmt.format(ind, self.format_metadata(value, indent + 4)) for value in data
        ]
        wrap = "(\n{}\n{})" if isinstance(data, tuple) else "[\n{}\n{}]"
        return wrap.format("\n".join(lines), ind)

    def format_string(self, data: str, indent: int, key: str = "", pad: int = 0) -> str:
        """
        Return a pretty string representation of a string.

        If the total length of the input string plus indent plus the key
        length and the additional pad is more than the max line length,
        wrap the text into multiple lines, avoiding breaking long words
        """
        if data.startswith("Type[") and data.endswith("]"):
            return data if data[5] == '"' else data[5:-1]

        if data.startswith("Literal[") and data.endswith("]"):
            return data[8:-1]

        if key in (self.FACTORY_KEY, self.DEFAULT_KEY):
            return data

        if key == "pattern":
            value = re.sub(r'([^\\])\"', r'\1\\"', data)
            return f'r"{value}"'
            # return f'r"{data}"'
        if data == "":
            return '""'

        start = indent + 2  # plus quotes
        start += len(key) + pad if key else 0

        if isinstance(data, Markup):
            value = data
        else:
            value = text.escape_string(data)
        length = len(value) + start
        if length < self.max_line_length or " " not in value:
            if isinstance(data, Markup):
                return value
            else:
                return f'"{value}"'

        if isinstance(data, Markup):
            return value
        else:
            next_indent = indent + 4
            value = "\n".join(
                f'{" " * next_indent}"{line}"'
                for line in textwrap.wrap(
                    value,
                    width=self.max_line_length - next_indent - 2,  # plus quotes
                    drop_whitespace=False,
                    replace_whitespace=False,
                    break_long_words=True,
                )
            )
            return f"(\n{value}\n{' ' * indent})"

    def text_wrap(self, string: str, offset: int = 0) -> str:
        """Wrap text in respect to the max line length and the given offset."""
        return "\n".join(
            textwrap.wrap(
                string,
                width=self.max_line_length - offset,
                drop_whitespace=True,
                replace_whitespace=True,
                break_long_words=False,
                subsequent_indent="    ",
            )
        )

    @classmethod
    def clean_docstring(cls, string: Optional[str], escape: bool = True) -> str:
        """
        Prepare string for docstring generation.

        - Strip whitespace from each line
        - Replace triple double quotes with single quotes
        - Escape backslashes

        :param string: input value
        :param escape: skip backslashes escape, if string is going to
            pass through formatting.
        """
        if not string:
            return ""

        def _clean(txt: str) -> str:
            if escape:
                txt = txt.replace("\\", "\\\\")

            return txt.replace('"""', "'''").strip()

        return "\n".join(_clean(line) for line in string.splitlines() if line.strip())

    def format_docstring(self, doc_string: str, level: int) -> str:
        """Format doc strings."""

        sep_pos = doc_string.rfind('"""')
        if sep_pos == -1:
            return ""

        content = doc_string[:sep_pos]
        params = doc_string[sep_pos + 3 :].strip()

        if content.strip() == '"""' and not params:
            return ""

        content += ' """' if content.endswith('"') else '"""'

        max_length = self.max_line_length - level * 4
        content = format_code(
            content,
            summary_wrap_length=max_length,
            description_wrap_length=max_length - 7,
            make_summary_multi_line=True,
        )

        if params:
            content = content.rstrip('"""').strip()
            new_lines = "\n" if content.endswith('"""') else "\n\n"
            content += f'{new_lines}{params}\n"""'

        return content

    def field_default_value(self, attr: Attr, ns_map: Optional[Dict] = None) -> Any:
        """Generate the field default value/factory for the given attribute."""
        if attr.is_list or (attr.is_tokens and not attr.default):
            return "tuple" if self.format.frozen else "list"
        if attr.is_dict:
            return "dict"
        if attr.default is None:
            return False if self.format.kw_only and not attr.is_optional else None
        if not isinstance(attr.default, str):
            return self.literal_value(attr.default)
        if attr.default.startswith("@enum@"):
            return self.field_default_enum(attr)

        types = converter.sort_types(attr.native_types)

        if attr.is_tokens:
            return self.field_default_tokens(attr, types, ns_map)

        return self.literal_value(
            converter.deserialize(
                attr.default, types, ns_map=ns_map, format=attr.restrictions.format
            )
        )

    def field_default_enum(self, attr: Attr) -> str:
        assert attr.default is not None

        qname, reference = attr.default[6:].split("::", 1)
        qname = next(x.alias or qname for x in attr.types if x.qname == qname)
        name = namespaces.local_name(qname)
        class_name = self.class_name(name)

        if attr.is_tokens:
            members = [
                f"Literal[{class_name}.{self.constant_name(member, name)}]"
                for member in reference.split("@")
            ]
            return f"lambda: {self.format_metadata(members, indent=8)}"

        return f"{class_name}.{self.constant_name(reference, name)}"

    def field_default_tokens(
        self, attr: Attr, types: List[Type], ns_map: Optional[Dict]
    ) -> str:
        assert isinstance(attr.default, str)

        fmt = attr.restrictions.format
        factory = tuple if self.format.frozen else list
        tokens = factory(
            converter.deserialize(val, types, ns_map=ns_map, format=fmt)
            for val in attr.default.split()
        )

        if attr.is_enumeration:
            return self.format_metadata(tuple(tokens), indent=8)

        return f"lambda: {self.format_metadata(tokens, indent=8)}"

    def is_complex_type(self, type_name: str) -> bool:
         return type_name not in ['bool', "int", "Decimal", "str", "dict", "object",
                                                 "float", "datetime", "XmlDateTime", "bytes"]

    def has_complex_types(self, type_names: List[str]):
        return bool(set(type_names) - {'bool', "int", "Decimal", "str", "dict", "object",
                                             "float", "datetime", "XmlDateTime", "bytes"})

    def type_names(self, attr: Attr, parents: List[str]) -> List[str]:
        return collections.unique_sequence(
            self.field_type_name(x, parents) for x in attr.types
        )

    def convert_primitive_types(self, type_names):
        """
        Converts certain types that Xsdata sets to the matching type in
        Python
        """
        for idx, type_name in enumerate(type_names):
            if type_name == "XmlDateTime":
                type_names[idx] = "datetime"

    def field_type(self, attr: Attr, parents: List[str]) -> str:
        """Generate type hints for the given attribute."""
        type_names = self.type_names(attr, parents)

        self.convert_primitive_types(type_names)


        result = ", ".join(type_names)
        if len(type_names) > 1:
            if self.has_complex_types(type_names):
                raise ValueError("Unsupported Union Types for Complex Types")
            result = f"Union[{result}]"
        elif self.has_complex_types(type_names):
            # don't use parents here because two different classes could
            # reference the same type
            attr_class = self.find_class_by_qname(attr.types[0].qname, parents)
            fqname = attr_class.fqname
            if fqname not in type_names[0]:
                result = fqname

        iterable = "Tuple[{}, ...]" if self.format.frozen else "List[{}]"

        if attr.is_tokens:
            result = iterable.format(result)

        if attr.is_list:
            return iterable.format(result)

        if attr.is_tokens:
            return result

        if attr.is_dict:
           raise ValueError("Dict is not supported type for DB")
           # return "Dict[str, str]"

        if attr.is_nillable or (
            attr.default is None and (attr.is_optional or not self.format.kw_only)
        ):
            return f"Optional[{result}]"

        return result

    def choice_type(self, choice: Attr, parents: List[str]) -> str:
        """
        Generate type hints for the given choice.

        Choices support a subset of features from normal attributes.
        First of all we don't have a proper type hint but a type
        metadata key. That's why we always need to wrap as Type[xxx].
        The second big difference is that our choice belongs to a
        compound field that might be a list, that's why list restriction
        is also ignored.
        """
        type_names = collections.unique_sequence(
            self.field_type_name(x, parents) for x in choice.types
        )

        result = ", ".join(type_names)
        if len(type_names) > 1:
            result = f"Union[{result}]"

        if choice.is_tokens:
            result = (
                f"Tuple[{result}, ...]" if self.format.frozen else f"List[{result}]"
            )

        return f"Type[{result}]"

    def field_type_name(self, attr_type: AttrType, parents: List[str]) -> str:
        name = self.type_name(attr_type)

        if attr_type.forward and attr_type.circular:
            outer_str = ".".join(map(self.class_name, parents))
            name = f'"{outer_str}"'
        elif attr_type.forward:
            outer_str = ".".join(map(self.class_name, parents))
            name = f'"{outer_str}.{name}"'
        elif attr_type.circular:
            name = f'"{name}"'

        return name

    def constant_value(self, attr: Attr) -> str:
        """Return the attr default value or type as constant value."""
        attr_type = attr.types[0]
        if attr_type.native:
            return f'"{attr.default}"'

        if attr_type.alias:
            return self.class_name(attr_type.alias)

        return self.type_name(attr_type)

    @classmethod
    def literal_value(cls, value: Any) -> str:
        if isinstance(value, str):
            return quoteattr(value)

        if isinstance(value, float):
            return str(value) if math.isfinite(value) else f'float("{value}")'

        if isinstance(value, QName):
            return f'QName("{value.text}")'

        return repr(value).replace("'", '"')

    def default_imports(self, output: str) -> str:
        """Generate the default imports for the given package output."""
        result = []
        for library, types in self.import_patterns.items():
            names = [
                name
                for name, searches in types.items()
                if any(search in output for search in searches)
            ]

            if len(names) == 1 and names[0] == "__module__":
                result.append(f"import {library}")
            elif names:
                result.append(f"from {library} import {', '.join(names)}")

        return "\n".join(result)

    @classmethod
    def build_import_patterns(cls) -> Dict[str, Dict]:
        type_patterns = cls.build_type_patterns
        return {
            "dataclasses": {"dataclass": ["@dataclass"], "field": [" = field("]},
            "decimal": {"Decimal": type_patterns("Decimal")},
            "enum": {"Enum": ["(Enum)"]},
            "typing": {
                "Dict": [": Dict"],
                "List": [": List["],
                "Optional": ["Optional["],
                "Tuple": ["Tuple["],
                "Type": ["Type["],
                "Union": ["Union["],
            },
            "xml.etree.ElementTree": {"QName": type_patterns("QName")},
            "xsdata.models.datatype": {
                "XmlDate": type_patterns("XmlDate"),
                "XmlDateTime": type_patterns("XmlDateTime"),
                "XmlDuration": type_patterns("XmlDuration"),
                "XmlPeriod": type_patterns("XmlPeriod"),
                "XmlTime": type_patterns("XmlTime"),
            },
        }

    @classmethod
    def build_type_patterns(cls, x: str) -> Tuple:
        return f": {x} =", f"[{x}]", f"[{x},", f" {x},", f" {x}]", f" {x}("
