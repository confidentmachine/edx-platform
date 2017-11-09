from django.conf import settings
from django.core.urlresolvers import reverse

from edxmako.shortcuts import marketing_link


def get_base_template_context():
    """Dict with entries needed for all templates that use the base template"""
    return {
        # Platform information
        'homepage_url': marketing_link('ROOT'),
        'dashboard_url': reverse('dashboard'),
        'template_revision': settings.EDX_PLATFORM_REVISION,
        'platform_name': settings.PLATFORM_NAME,
        'contact_mailing_address': settings.CONTACT_MAILING_ADDRESS,
        'social_media_urls': getattr(settings, 'SOCIAL_MEDIA_FOOTER_URLS', {}),
        'mobile_store_urls': getattr(settings, 'MOBILE_STORE_URLS', {}),
    }
