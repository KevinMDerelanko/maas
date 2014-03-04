# Copyright 2012-2014 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Define json schema for power parameters."""

from __future__ import (
    absolute_import,
    print_function,
    unicode_literals,
    )

str = None

__metaclass__ = type
__all__ = [
    "make_json_field",
    "JSON_POWER_TYPE_PARAMETERS_SCHEMA",
    "POWER_TYPE_PARAMETER_FIELD_SCHEMA",
    ]


from jsonschema import validate

# Represent the Django choices format as JSON; an array of 2-item
# arrays.
CHOICE_FIELD_SCHEMA = {
    'type': 'array',
    'items': {
        'title': "Power type paramter field choice",
        'type': 'array',
        'minItems': 2,
        'maxItems': 2,
        'uniqueItems': True,
        'items': {
            'type': 'string',
        }
    },
}


POWER_TYPE_PARAMETER_FIELD_SCHEMA = {
    'title': "Power type parameter field",
    'type': 'object',
    'properties': {
        'name': {
            'type': 'string',
        },
        'field_type': {
            'type': 'string',
        },
        'label': {
            'type': 'string',
        },
        'required': {
            'type': 'boolean',
        },
        'choices': CHOICE_FIELD_SCHEMA,
        'default': {
            'type': 'string',
        },
    },
    'required': ['field_type', 'label', 'required'],
}


# A basic JSON schema for what power type parameters should look like.
JSON_POWER_TYPE_PARAMETERS_SCHEMA = {
    'title': "Power parameters set",
    'type': 'array',
    'items': {
        'title': "Power type parameters",
        'type': 'object',
        'properties': {
            'name': {
                'type': 'string',
            },
            'fields': {
                'type': 'array',
                'items': POWER_TYPE_PARAMETER_FIELD_SCHEMA,
            },
        },
        'required': ['name', 'fields'],
    },
}


def make_json_field(
        name, label, field_type=None, choices=None, default=None,
        required=False):
    """Helper function for building a JSON power type parameters field.

    :param name: The name of the field.
    :type name: string
    :param label: The label to be presented to the user for this field.
    :type label: string
    :param field_type: The type of field to create. Can be one of
        (string, choice, mac_address). Defaults to string.
    :type field_type: string.
    :param choices: The collection of choices to present to the user.
        Needs to be structured as a list of lists, otherwise
        make_json_field() will raise a ValidationError.
    :type list:
    :param default: The default value for the field.
    :type default: string
    :param required: Whether or not a value for the field is required.
    :type required: boolean
    """
    if field_type not in ('string', 'mac_address', 'choice'):
        field_type = 'string'
    if choices is None:
        choices = []
    validate(choices, CHOICE_FIELD_SCHEMA)
    if default is None:
        default = ""
    field = {
        'name': name,
        'label': label,
        'required': required,
        'field_type': field_type,
        'choices': choices,
        'default': default,
    }
    return field
