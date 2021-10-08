import re
import textwrap
from typing import List, Optional, Dict, Union, Set

from jinja2 import Environment
from markupsafe import Markup

from xsdata.codegen.models import Class, Attr, Extension, AttrType

from xsdata.formats.dataclass.filters import Filters
from xsdata.utils import collections, text


class SqlAlchemyTemplateFilters(Filters):

    __slots__ = list(Filters.__slots__) + ["classes", "fqname_map"]

    def set_classes(self, classes: List[Class]) -> None:
        self.classes = classes
        self.fqname_map: Dict[str, Class] = {}
        self.set_class_fully_qualified_names(self.classes)

    def set_class_fully_qualified_names(self, classes, parent_fqdn=None):
        for clazz in classes:
            class_name = self.class_name(clazz.qname)
            fqdn = ".".join([parent_fqdn, class_name]) if parent_fqdn else class_name
            for val in self.fqname_map.values():
                if clazz == val:
                   continue
            self.fqname_map[fqdn] = clazz

            if clazz.inner:
                self.set_class_fully_qualified_names(clazz.inner, fqdn)

    def register(self, env: Environment):
        super().register(env)
        env.filters.update({
            "extension_attrs": self.extension_attrs,
            "relationship_backrefs": self.relationship_backrefs,
            "table_name": self.table_name,
            "is_many_to_one": self.is_many_to_one,
            "relationship_definition": self.relationship_definition
        })

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
            return self.find_class_by_qname(extension.type.qname, [])[1]


    def extension_attrs(self, extensions: List[Extension]) -> List[Attr]:
        attrs = []
        for extension in extensions:
            clazz = self.get_class_for_extension(extension)
            attrs += clazz.attrs

        return attrs

    def field_metadata(
        self, attr: Attr, parent_namespace: Optional[str], parents: List[str]
    ) -> Dict:
        metadata = super().field_metadata(attr, parent_namespace, parents)
        metadata["sa"] = self.sql_alchemy_column(metadata.get("name", None), attr, parent_namespace, parents)
        return self.filter_metadata(metadata)

    def sql_alchemy_column(self, name: str, attr: Attr, parent_namespace: Optional[str], parents: List[str]) -> str:
        # TODO attr.restrictions
        column_fmt = "Column({})"

        type_names = self.type_names(attr, parents)
        type_name = type_names[0]

        postgres_datatype: Optional[str] = None
        if len(type_names) > 1:
            raise ValueError("Multiple value types for DB storage is unsupported")
        if type_name in ("dict", "object"):
            postgres_datatype = "JSONB"
        elif type_name == "bytes":
            postgres_datatype= "LargeBinary()"
        elif type_name == "datetime" or type_name == "XmlDateTime":
            postgres_datatype = "SqlXmlDateTime"
        elif type_name == "bool":
            postgres_datatype = "Boolean"
        elif type_name == "int":
            postgres_datatype = "Integer"
        elif type_name == "Decimal":
            postgres_datatype = "Numeric"
        elif type_name == "str":
            postgres_datatype= "String"
        elif self.has_complex_types(type_names):
            attr_fqname, attr_class = self.find_class_by_qname(attr.types[0].qname, parents)
            if attr_class.is_enumeration:
                return Markup(column_fmt.format(f"SqlEnum({type_name}, name=\"{'_'.join([type_name]+parents)}\", inherit_schema=True)"))
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

        if not self.has_complex_types(type_names):
            # use postgresql arrays for lists of primitive types
            if attr.is_list:
                return Markup(column_fmt.format(f"ARRAY({postgres_datatype})"))
            else:
                return Markup(column_fmt.format(postgres_datatype))

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

    def field_type(self, attr: Attr, parents: List[str]) -> str:
        """Generate type hints for the given attribute."""
        type_names = self.type_names(attr, parents)

        result = ", ".join(type_names)
        if len(type_names) > 1:
            if self.has_complex_types(type_names):
                raise ValueError("Unsupported Union Types for Complex Types")
            result = f"Union[{result}]"
        elif self.has_complex_types(type_names):
            # don't use parents here because two different classes could
            # reference the same type
            fqname, attr_class = self.find_class_by_qname(attr.types[0].qname, parents)
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


    def build_backwards_relationships(self, obj, classes: List[Class], relationships: List[str], parents: List[str]) -> List[str]:
        """
        Finds all other classes that have a relationship to obj recursively and
        adds a back_populating relationship field
        """
        for clazz in classes:
            if clazz == obj:
                continue
            for ref_attr in clazz.attrs:
                ref_attr_types = self.type_names(ref_attr, [self.class_name(clazz.qname)])
                if not self.has_complex_types(ref_attr_types):
                    continue

                ref_attr_fqname, ref_attr_class = self.find_class_by_qname(ref_attr.types[0].qname, [clazz.qname])
                obj_fqname = self.find_fqname_by_class(obj)
                if ref_attr_fqname == obj_fqname:
                    if ref_attr.is_list:
                        full_class_name = self.find_fqname_by_class(clazz)
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

    def is_many_to_one(self, attr, parents):
        type_names = self.type_names(attr, parents)
        if self.has_complex_types(type_names):
            _, attr_class = self.find_class_by_qname(attr.types[0].qname, parents)
            return not (attr.is_list or attr.is_enumeration or attr_class.is_enumeration)

    def relationship_definition(self,         attr: Attr,
        ns_map: Dict,
        parent_namespace: Optional[str],
        parents: List[str],):
        fqname, attr_class = self.find_class_by_qname(attr.types[0].qname, parents)
        table_name = self.table_name(fqname.split("."))
        return 'field(default=None, metadata={{"sa": Column(ForeignKey(\"{}.id\", use_alter=True))}})'.format(table_name)

    # def build_relationships(self, obj: Class, relationships: List[str], parents: List[str]) -> None:
    #     for attr in obj.attrs:
    #         attr_types = self.type_names(attr, [])
    #         if not self.has_complex_types(attr_types):
    #             continue
    #         attr_class = self.find_class_by_qname(attr.types[0].qname, parents)
    #         if attr.is_list or attr_class.is_enumeration or attr.is_enumeration:
    #             # list attributes are already handled by the normal field population
    #             continue
    #         else:
    #             full_class_name = attr_class.fqname if attr_class.fqname else self.fqname(attr_class)
    #             table_name = self.table_name(full_class_name.split("."))
    #             relationships.append(
    #                 '{}_id: int =
    #             # relationships.append(
    #             #     "{name}: \"{fqname}\" = relationship(\"{fqname}\", back_populates=\"{attr_name}\")".format(
    #             #         name=self.field_case(attr.name),
    #             #         fqname=self.class_name(full_class_name),
    #             #         attr_name=self.field_case(obj.qname)
    #             #     ))


    def relationship_backrefs(self,
        obj: Class,
        parents: List[str]
    ) -> List[str]:
        relationships = []
        #self.build_relationships(obj, relationships, parents)
        self.build_backwards_relationships(obj, self.classes, relationships, parents)
        # find all classes that have a list reference to it
        return relationships

    # def _fqname_recursive(self, obj: Class, class_list: List[Class]) -> str:
    #     for clazz in class_list:
    #         if clazz.qname == obj.qname:
    #             return obj.qname
    #         elif clazz.inner:
    #             inner_clazz_result = self._fqname_recursive(obj, clazz.inner)
    #             if inner_clazz_result:
    #                 return clazz.qname + "." + inner_clazz_result

    def find_fqname_by_class(self, obj: Class) -> str:
        for fqname, clazz in self.fqname_map.items():
            if id(obj) == id(clazz):
                return fqname

    # def _compare_class(self, hierachy: str, clazz: Class, qname: str) -> Class:
    #     if hierachy:
    #         hierachy += "." + self.class_name(clazz.qname)
    #     else:
    #         hierachy = self.class_name(clazz.qname)
    #     if hierachy.split(".")[-1] == self.class_name(qname):
    #         clazz.fqname = hierachy
    #         return clazz
    #     for inner_clazz in clazz.inner:
    #         match = self._compare_class(hierachy, inner_clazz, qname)
    #         if match:
    #             return match
    #         else:
    #             self._compare_class(hierachy, inner_clazz, qname.split(".")[-1])
    #             if match:
    #                 return match

    def find_class_by_qname(self, qname: str, parents: List[str]) -> (str, Class):
        # first find the class for the parents and prefer to grab the class from a
        # defined inner class
        class_name = self.class_name(qname)
        possible_fqdn = ".".join([self.class_name(p) for p in parents] + [class_name])
        if possible_fqdn in self.fqname_map:
            return possible_fqdn, self.fqname_map[possible_fqdn]
        elif class_name in self.fqname_map:
            return class_name, self.fqname_map[class_name]
        else:
            for fqdn, clazz in self.fqname_map.items():
                if fqdn.endswith(possible_fqdn):
                    return fqdn, clazz
                elif fqdn.endswith(class_name):
                    return fqdn, clazz
        raise ValueError(f"Can't find class for qname {qname}")

        # for clazz in self.classes:
        #     if clazz.qname in parents:
        #         match = self._compare_class("", clazz, qname)
        #         if match:
        #             return match
        # # else, try to find any matching class for the attr
        # for clazz in self.classes:
        #     match = self._compare_class("", clazz, qname)
        #     if match:
        #         return match


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