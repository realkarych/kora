from collections.abc import Callable, Generator, Iterator, Mapping
from fractions import Fraction
from typing import Never, Protocol, cast

from jsonschema import (
    Draft202012Validator,
    TypeChecker,
    ValidationError,
    _utils,
    validators,
)
from referencing import Registry, Resource, Specification
from referencing._core import Resolver
from referencing.jsonschema import DRAFT202012

from kora.ir.standard_schema_source import Sources
from kora.ir.standard_schema_values import View

ORDINALS: tuple[str, ...] = tuple(f"urn:kora:schema:{i}" for i in range(2048))
SPECIFICATION = cast("Specification[View]", DRAFT202012)
EQUAL = cast("Callable[[View, View], bool]", object.__getattribute__(_utils, "equal"))


class Engine(Protocol):
    def evolve(self, *, schema: View) -> "Engine": ...

    def iter_errors(self, instance: View) -> Generator[ValidationError, None, None]: ...


class Factory(Protocol):
    def __call__(
        self, schema: View, *, registry: Registry[View], _resolver: Resolver[View]
    ) -> Engine: ...


Handler = Callable[[Engine, View, View, View], Iterator[ValidationError]]


class Extend(Protocol):
    def __call__(
        self,
        validator: object,
        validators: Mapping[str, Handler],
        *,
        type_checker: TypeChecker,
    ) -> Factory: ...


def boolean_decision(engine: Engine, instance: View) -> bool:
    errors = engine.iter_errors(instance)
    try:
        return next(errors, None) is None
    finally:
        errors.close()


def any_of(
    engine: Engine, argument: View, instance: View, _schema: View
) -> Iterator[ValidationError]:
    for schema in cast("list[View]", argument):
        if boolean_decision(engine.evolve(schema=schema), instance):
            return
    yield ValidationError("schema mismatch")


def one_of(
    engine: Engine, argument: View, instance: View, _schema: View
) -> Iterator[ValidationError]:
    matched = 0
    for schema in cast("list[View]", argument):
        matched += boolean_decision(engine.evolve(schema=schema), instance)
        if matched == 2:
            break
    if matched != 1:
        yield ValidationError("schema mismatch")


def unique_items(
    _engine: Engine, argument: View, instance: View, _schema: View
) -> Iterator[ValidationError]:
    if argument is True and type(instance) is list:
        for index, item in enumerate(instance):
            for previous in range(index):
                if EQUAL(item, instance[previous]):
                    yield ValidationError("schema mismatch")
                    return


def is_number(_checker: TypeChecker, instance: object) -> bool:
    return type(instance) is int or type(instance) is Fraction


def is_integer(_checker: TypeChecker, instance: object) -> bool:
    return type(instance) is int or (
        type(instance) is Fraction and instance.denominator == 1
    )


CHECKER = (
    cast("TypeChecker", Draft202012Validator.TYPE_CHECKER)
    .redefine("number", is_number)
    .redefine("integer", is_integer)
)
ENGINE: Factory = cast("Extend", object.__getattribute__(validators, "extend"))(
    Draft202012Validator,
    {"anyOf": any_of, "oneOf": one_of, "uniqueItems": unique_items},
    type_checker=CHECKER,
)


def numerical_view(value: object) -> View:
    if type(value) is float:
        return Fraction(repr(value))
    if type(value) is list:
        return [numerical_view(item) for item in cast("list[object]", value)]
    if type(value) is dict:
        return {
            key: numerical_view(item)
            for key, item in cast("dict[str, object]", value).items()
        }
    return cast("None | bool | int | str", value)


def schema_views(source: object, sources: Sources) -> list[View]:
    normalized = cast("dict[str, View]", numerical_view(source))
    documents = cast("list[dict[str, View]]", normalized["documents"])
    views: list[View] = []
    for index, site in enumerate(sources.sites):
        if site.parent < 0:
            view = documents[site.document]["schema"]
        else:
            view = cast("dict[str, View]", views[site.parent])[site.keyword]
            if type(site.segment) is str:
                view = cast("dict[str, View]", view)[site.segment]
            elif type(site.segment) is int:
                view = cast("list[View]", view)[site.segment]
        if type(view) is dict:
            for marker in ("$schema", "$id", "$anchor", "$dynamicAnchor"):
                view.pop(marker, None)
            for keyword in ("$ref", "$dynamicRef"):
                if keyword in view:
                    view[keyword] = ORDINALS[sources.references[index, keyword]]
        views.append(view)
    return views


def refuse_retrieval(uri: str) -> Never:
    raise RuntimeError("schema registry retrieval unavailable")


def evaluate(value: object, source: object, sources: Sources) -> bool:
    view = numerical_view(value)
    schemas = schema_views(source, sources)
    registry = Registry[View](retrieve=refuse_retrieval).with_resources(
        (ORDINALS[index], Resource(schema, SPECIFICATION))
        for index, schema in enumerate(schemas)
    )
    for root in sources.roots():
        engine = ENGINE(
            schemas[root],
            registry=registry,
            _resolver=registry.resolver(ORDINALS[root]),
        )
        if not boolean_decision(engine, view):
            return False
    return True
