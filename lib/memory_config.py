"""Validate project memory settings before the installer changes any service."""
import re


def validate_settings(settings, gateway):
    if not isinstance(settings, dict) or set(settings) - {
        'model', 'embedding_model', 'embedding_dimensions',
    }:
        raise ValueError('project_memory contains unsupported settings')
    model = settings.get('model')
    if not isinstance(model, str) or not gateway or model not in gateway['models']:
        raise ValueError('project_memory.model must name a configured gateway model')
    embedding = settings.get('embedding_model')
    if not isinstance(embedding, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9/_.:-]{0,159}', embedding):
        raise ValueError('project_memory.embedding_model is required')
    dimensions = settings.get('embedding_dimensions', 1536)
    if type(dimensions) is not int or not 1 <= dimensions <= 4096:
        raise ValueError('project_memory.embedding_dimensions must be an integer from 1 to 4096')
    upstream = gateway['models'][model]
    provider = gateway['providers'][upstream['provider']]
    return {
        'base_url': provider['base_url'], 'key_file': provider['key_file'],
        'model': upstream['model'], 'embedding_model': embedding,
        'embedding_dimensions': dimensions,
    }
