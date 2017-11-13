""" API v0 views. """
import logging

from edx_rest_api_client import exceptions
from opaque_keys import InvalidKeyError
from opaque_keys.edx.keys import CourseKey
from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.status import HTTP_406_NOT_ACCEPTABLE, HTTP_409_CONFLICT
from rest_framework.views import APIView

from commerce.constants import Messages
from commerce.http import DetailResponse
from course_modes.models import CourseMode
from courseware import courses
from enrollment.api import add_enrollment
from enrollment.views import EnrollmentCrossDomainSessionAuth
from entitlements.models import CourseEntitlement
from openedx.core.djangoapps.commerce.utils import ecommerce_api_client
from openedx.core.djangoapps.embargo import api as embargo_api
from openedx.core.djangoapps.user_api.preferences.api import update_email_opt_in
from openedx.core.lib.api.authentication import OAuth2AuthenticationAllowInactiveUser
from student.models import CourseEnrollment
from util.json_request import JsonResponse

log = logging.getLogger(__name__)
SAILTHRU_CAMPAIGN_COOKIE = 'sailthru_bid'


class BasketsView(APIView):
    """ Creates a basket with a course seat and enrolls users. """

    # LMS utilizes User.user_is_active to indicate email verification, not whether an account is active. Sigh!
    authentication_classes = (EnrollmentCrossDomainSessionAuth, OAuth2AuthenticationAllowInactiveUser)
    permission_classes = (IsAuthenticated,)

    def _is_data_valid(self, request):
        """
        Validates the data posted to the view.

        Arguments
            request -- HTTP request

        Returns
            Tuple (data_is_valid, course_key, error_msg)
        """
        course_id = request.data.get('course_id')

        if not course_id:
            return False, None, u'Field course_id is missing.'

        try:
            course_key = CourseKey.from_string(course_id)
            courses.get_course(course_key)
        except (InvalidKeyError, ValueError)as ex:
            log.exception(u'Unable to locate course matching %s.', course_id)
            return False, None, ex.message

        return True, course_key, None

    def _enroll(self, course_key, user, mode=CourseMode.DEFAULT_MODE_SLUG):
        """ Enroll the user in the course. """
        add_enrollment(user.username, unicode(course_key), mode)

    def _handle_marketing_opt_in(self, request, course_key, user):
        """
        Handle the marketing email opt-in flag, if it was set.

        Errors here aren't expected, but should not break the outer enrollment transaction.
        """
        email_opt_in = request.data.get('email_opt_in', None)
        if email_opt_in is not None:
            try:
                update_email_opt_in(user, course_key.org, email_opt_in)
            except Exception:  # pylint: disable=broad-except
                # log the error, return silently
                log.exception(
                    'Failed to handle marketing opt-in flag: user="%s", course="%s"', user.username, course_key
                )

    def post(self, request, *args, **kwargs):
        """
        Attempt to enroll the user.
        """
        course_entitlement = None
        user = request.user
        valid, course_run_key, error = self._is_data_valid(request)
        course_uuid = request.data.get('course_uuid', None)
        if not valid:
            return DetailResponse(error, status=HTTP_406_NOT_ACCEPTABLE)

        embargo_response = embargo_api.get_embargo_response(request, course_run_key, user)

        if embargo_response:
            return embargo_response

        if course_uuid:
            course_entitlement = CourseEntitlement.get_open_user_course_entitlements(user, course_uuid)

        # Don't do anything if an enrollment already exists
        course_run_id = unicode(course_run_key)
        enrollment = CourseEnrollment.get_enrollment(user, course_run_key)
        if enrollment and enrollment.is_active:
            msg = Messages.ENROLLMENT_EXISTS.format(course_id=course_run_id, username=user.username)
            return DetailResponse(msg, status=HTTP_409_CONFLICT)

        # Check to see if enrollment for this course is closed.
        course = courses.get_course(course_run_key)
        if CourseEnrollment.is_enrollment_closed(user, course):
            msg = Messages.ENROLLMENT_CLOSED.format(course_id=course_run_id)
            log.info(u'Unable to enroll user %s in closed course %s.', user.id, course_run_id)
            return DetailResponse(msg, status=HTTP_406_NOT_ACCEPTABLE)

        if course_entitlement:
            msg = Messages.ENROLL_DIRECTLY.format(
                username=user.username,
                course_id=course_run_id
            )
            log.info(msg)
            self._enroll(course_run_key, user, course_entitlement.mode)
            self._handle_marketing_opt_in(request, course_run_key, user)
            CourseEntitlement.set_enrollment(
                entitlement=course_entitlement,
                enrollment=CourseEnrollment.get_enrollment(user, course_run_key)
            )
            return DetailResponse(msg)

        # If there is no audit or honor course mode, this most likely
        # a Prof-Ed course. Return an error so that the JS redirects
        # to track selection.
        honor_mode = CourseMode.mode_for_course(course_run_key, CourseMode.HONOR)
        audit_mode = CourseMode.mode_for_course(course_run_key, CourseMode.AUDIT)

        # Accept either honor or audit as an enrollment mode to
        # maintain backwards compatibility with existing courses
        default_enrollment_mode = audit_mode or honor_mode
        if default_enrollment_mode:
            msg = Messages.ENROLL_DIRECTLY.format(
                username=user.username,
                course_id=course_run_id
            )
            if not default_enrollment_mode.sku:
                # If there are no course modes with SKUs, return a different message.
                msg = Messages.NO_SKU_ENROLLED.format(
                    enrollment_mode=default_enrollment_mode.slug,
                    course_id=course_run_id,
                    username=user.username
                )
            log.info(msg)
            self._enroll(course_run_key, user, default_enrollment_mode.slug)
            self._handle_marketing_opt_in(request, course_run_key, user)
            return DetailResponse(msg)
        else:
            msg = Messages.NO_DEFAULT_ENROLLMENT_MODE.format(course_id=course_run_id)
            return DetailResponse(msg, status=HTTP_406_NOT_ACCEPTABLE)


class BasketOrderView(APIView):
    """ Retrieve the order associated with a basket. """

    authentication_classes = (SessionAuthentication,)
    permission_classes = (IsAuthenticated,)

    def get(self, request, *_args, **kwargs):
        """ HTTP handler. """
        try:
            order = ecommerce_api_client(request.user).baskets(kwargs['basket_id']).order.get()
            return JsonResponse(order)
        except exceptions.HttpNotFoundError:
            return JsonResponse(status=404)
