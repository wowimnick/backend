"""
Factory Boy factories for quickstart models.
Used to create test data for public endpoint tests; data is isolated per test via DB transactions.
"""
import uuid
from decimal import Decimal
from datetime import date, time, timedelta
from django.utils import timezone

import factory
from factory.django import DjangoModelFactory

from quickstart.models import (
    BlogCategory,
    BlogPost,
    Booking,
    BusinessAddonSubscription,
    BusinessEmailCampaign,
    BusinessInfo,
    BusinessMarketingSettings,
    BusinessRole,
    BusinessStaff,
    ClassCategory,
    ClassOption,
    ClassesMain,
    ClassSubcategory,
    Contact,
    Conversation,
    ConversationMessage,
    CustomUser,
    CustomerMembership,
    Discount,
    EmailMarketingTemplate,
    GiftCard,
    MembershipProduct,
    NotificationCampaign,
    PartnerTier,
    Payment,
    Payout,
    Reviews,
    Role,
    Schedule,
    ScheduleInstance,
    SupportTicket,
    TicketMessage,
    WidgetSubscription,
)


class UserFactory(DjangoModelFactory):
    class Meta:
        model = CustomUser
        django_get_or_create = ("email",)

    username = factory.Sequence(lambda n: f"user{n}@example.com")
    email = factory.Sequence(lambda n: f"user{n}@example.com")
    first_name = factory.Faker("first_name")
    last_name = factory.Faker("last_name")
    country = "Canada"
    city = "Toronto"
    state = "ON"
    address = "123 Test St"
    zipCode = "M5V 1A1"
    is_active = True
    is_staff = False
    is_superuser = False


class PartnerTierFactory(DjangoModelFactory):
    class Meta:
        model = PartnerTier
        django_get_or_create = ("name",)

    name = factory.Sequence(lambda n: f"Tier {n}")
    fee_percentage = Decimal("13.00")
    is_default = False


class BusinessFactory(DjangoModelFactory):
    class Meta:
        model = BusinessInfo

    owner = factory.SubFactory(UserFactory)
    businessName = factory.Sequence(lambda n: f"Test Business {n}")
    slug = factory.Sequence(lambda n: f"test-business-{n}")
    businessType = "venue"
    businessDescription = "Test description"
    studentContactPhone = "+15551234567"
    studentContactEmail = factory.LazyAttribute(lambda o: o.owner.email)
    preferredContact = "email"
    businessAddress = "123 Main St"
    businessCity = "Toronto"
    businessState = "ON"
    businessZipCode = "M5V 1A1"
    isActive = True
    verificationStatus = "verified"
    termsAccepted = True
    privacyAccepted = True
    business_timezone = "America/Toronto"


class ContactFactory(DjangoModelFactory):
    class Meta:
        model = Contact

    business = factory.SubFactory(BusinessFactory)
    first_name = factory.Faker("first_name")
    last_name = factory.Faker("last_name")
    email = factory.Sequence(lambda n: f"guest{n}@example.com")
    phone_number = "+15559876543"
    source = "guest_booking"
    user = None


class ClassCategoryFactory(DjangoModelFactory):
    class Meta:
        model = ClassCategory
        django_get_or_create = ("key",)

    name = factory.Sequence(lambda n: f"Category {n}")
    key = factory.Sequence(lambda n: f"category-{n}")
    is_featured = False
    sort_order = 0


class ClassSubcategoryFactory(DjangoModelFactory):
    class Meta:
        model = ClassSubcategory

    category = factory.SubFactory(ClassCategoryFactory)
    name = factory.Sequence(lambda n: f"Subcategory {n}")
    key = factory.Sequence(lambda n: f"subcat-{n}")


class ClassMainFactory(DjangoModelFactory):
    class Meta:
        model = ClassesMain

    businessId = factory.SubFactory(BusinessFactory)
    title = factory.Sequence(lambda n: f"Test Class {n}")
    slug = factory.Sequence(lambda n: f"test-class-{n}")
    description = "A test class description."
    features = []
    status = "active"
    location = "123 Studio St"
    city = "Toronto"
    state = "ON"
    coordinates = "43.6532,-79.3832"


class ClassOptionFactory(DjangoModelFactory):
    class Meta:
        model = ClassOption

    classId = factory.SubFactory(ClassMainFactory)
    title = "General Admission"
    booking_type = "Single Session"
    cancellationPolicy = "flexible"
    price_type = "per_session"
    level = "all"


class ScheduleFactory(DjangoModelFactory):
    class Meta:
        model = Schedule

    option = factory.SubFactory(ClassOptionFactory)
    # Single-session Schedule.clean() requires date; day is set from date in save().
    date = factory.LazyFunction(lambda: date.today() + timedelta(days=7))
    day = "Sat"
    time = time(10, 0)
    duration = 60
    price = Decimal("25.00")
    maxParticipants = 10
    minParticipants = 1


class ScheduleInstanceFactory(DjangoModelFactory):
    class Meta:
        model = ScheduleInstance

    schedule = factory.SubFactory(ScheduleFactory)
    date = factory.LazyFunction(lambda: date.today() + timedelta(days=7))
    time = time(10, 0)
    duration = 60
    price = Decimal("25.00")
    max_participants = 10
    min_participants = 1
    status = "scheduled"


class BookingFactory(DjangoModelFactory):
    class Meta:
        model = Booking

    schedule_instance = factory.SubFactory(ScheduleInstanceFactory)
    user = None
    contact = factory.SubFactory(ContactFactory)
    participants = 1
    amount_paid = Decimal("25.00")
    payment_status = "paid"
    status = "confirmed"
    enrollment_type = "Single Session"
    cancellation_policy = "flexible"
    cancellation_token = factory.LazyFunction(uuid.uuid4)

    @factory.post_generation
    def link_contact_to_business(obj, create, extracted, **kwargs):
        if create and obj.contact and obj.schedule_instance:
            business = obj.schedule_instance.schedule.option.classId.businessId
            obj.contact.business = business
            obj.contact.save(update_fields=["business"])


class GiftCardFactory(DjangoModelFactory):
    class Meta:
        model = GiftCard

    initial_amount = Decimal("50.00")
    current_balance = Decimal("50.00")
    currency = "CAD"
    recipient_email = factory.Faker("email")
    recipient_name = factory.Faker("name")
    sender_name = factory.Faker("name")
    message = "Happy testing!"
    is_active = True
    code = factory.LazyFunction(
        lambda: "TEST-" + uuid.uuid4().hex[:4].upper() + "-" + uuid.uuid4().hex[:4].upper()
    )


class BlogCategoryFactory(DjangoModelFactory):
    class Meta:
        model = BlogCategory
        django_get_or_create = ("slug",)

    name = factory.Sequence(lambda n: f"Blog Category {n}")
    slug = factory.Sequence(lambda n: f"blog-cat-{n}")


class BlogPostFactory(DjangoModelFactory):
    class Meta:
        model = BlogPost

    title = factory.Sequence(lambda n: f"Blog Post {n}")
    slug = factory.Sequence(lambda n: f"blog-post-{n}")
    excerpt = "Short excerpt."
    content = "<p>Full content here.</p>"
    image_url = "https://example.com/image.jpg"
    author = factory.SubFactory(UserFactory)
    category = factory.SubFactory(BlogCategoryFactory)
    status = "published"
    tags = []


class RoleFactory(DjangoModelFactory):
    class Meta:
        model = Role
        django_get_or_create = ("name",)

    name = factory.Sequence(lambda n: f"test-role-{n}")
    is_system = False
    is_default = False
    hierarchy_level = 0


class BusinessRoleFactory(DjangoModelFactory):
    class Meta:
        model = BusinessRole

    business = factory.SubFactory(BusinessFactory)
    name = factory.Sequence(lambda n: f"Business Role {n}")
    description = ""


class BusinessStaffFactory(DjangoModelFactory):
    class Meta:
        model = BusinessStaff

    business = factory.SubFactory(BusinessFactory)
    role = factory.LazyAttribute(lambda o: BusinessRoleFactory(business=o.business))
    user = factory.SubFactory(UserFactory)
    invited_email = factory.LazyAttribute(lambda o: o.user.email)
    status = BusinessStaff.StaffStatus.ACCEPTED
    invited_by = factory.LazyAttribute(lambda o: o.business.owner)


class DiscountFactory(DjangoModelFactory):
    class Meta:
        model = Discount

    business = factory.SubFactory(BusinessFactory)
    name = factory.Sequence(lambda n: f"Discount {n}")
    code = factory.Sequence(lambda n: f"SAVE{n}")
    discount_type = Discount.DiscountType.PERCENTAGE
    value = Decimal("10.00")
    scope = Discount.DiscountScope.BUSINESS
    is_active = True


class PaymentFactory(DjangoModelFactory):
    class Meta:
        model = Payment

    booking = factory.SubFactory(BookingFactory)
    stripe_payment_intent_id = factory.Sequence(lambda n: f"pi_test_{n}_{uuid.uuid4().hex[:8]}")
    amount = Decimal("25.00")
    status = "succeeded"
    currency = "CAD"


class PayoutFactory(DjangoModelFactory):
    class Meta:
        model = Payout

    business = factory.SubFactory(BusinessFactory)
    stripe_transfer_id = factory.Sequence(lambda n: f"tr_test_{n}_{uuid.uuid4().hex[:8]}")
    amount = Decimal("100.00")
    currency = "CAD"
    status = "pending"


class WidgetSubscriptionFactory(DjangoModelFactory):
    class Meta:
        model = WidgetSubscription

    business = factory.SubFactory(BusinessFactory)
    plan_id = "basic"
    status = "active"
    stripe_subscription_id = factory.Sequence(lambda n: f"sub_widget_{n}_{uuid.uuid4().hex[:8]}")


class BusinessAddonSubscriptionFactory(DjangoModelFactory):
    class Meta:
        model = BusinessAddonSubscription

    business = factory.SubFactory(BusinessFactory)
    addon_type = "marketplace_email_branding"
    status = "active"
    stripe_subscription_id = factory.Sequence(
        lambda n: f"sub_addon_{n}_{uuid.uuid4().hex[:8]}"
    )


class BusinessMarketingSettingsFactory(DjangoModelFactory):
    class Meta:
        model = BusinessMarketingSettings

    business = factory.SubFactory(BusinessFactory)


class EmailMarketingTemplateFactory(DjangoModelFactory):
    class Meta:
        model = EmailMarketingTemplate

    business = factory.SubFactory(BusinessFactory)
    name = factory.Sequence(lambda n: f"Template {n}")
    subject = "Hello"
    html_body = "<p>Test</p>"


class BusinessEmailCampaignFactory(DjangoModelFactory):
    class Meta:
        model = BusinessEmailCampaign

    business = factory.SubFactory(BusinessFactory)
    name = factory.Sequence(lambda n: f"Campaign {n}")
    status = "draft"
    subject = "Subject"
    html_body = "<p>Body</p>"


class NotificationCampaignFactory(DjangoModelFactory):
    class Meta:
        model = NotificationCampaign

    title = factory.Sequence(lambda n: f"Notif Campaign {n}")
    notification_type = "email"
    subject = "Subject"
    content = "Content"
    audience_type = "all_users"
    status = "draft"
    created_by = factory.SubFactory(UserFactory)


class MembershipProductFactory(DjangoModelFactory):
    class Meta:
        model = MembershipProduct

    business = factory.SubFactory(BusinessFactory)
    name = factory.Sequence(lambda n: f"Membership {n}")
    price = Decimal("29.99")
    currency = "CAD"
    billing_interval = "month"
    access_type = "unlimited"
    is_active = True


class CustomerMembershipFactory(DjangoModelFactory):
    class Meta:
        model = CustomerMembership

    product = factory.SubFactory(MembershipProductFactory)
    contact = factory.LazyAttribute(
        lambda o: ContactFactory(business=o.product.business)
    )
    status = "active"
    source = "manual"


class ReviewsFactory(DjangoModelFactory):
    class Meta:
        model = Reviews

    userId = factory.SubFactory(UserFactory)
    businessId = factory.LazyAttribute(lambda o: o.classId.businessId)
    classId = factory.SubFactory(ClassMainFactory)
    rating = 5
    comment = "Great class!"
    status = "approved"


class SupportTicketFactory(DjangoModelFactory):
    class Meta:
        model = SupportTicket

    user = factory.SubFactory(UserFactory)
    subject = factory.Sequence(lambda n: f"Support issue {n}")
    description = "Need help"
    category = "other"
    status = "open"
    priority = "medium"


class TicketMessageFactory(DjangoModelFactory):
    class Meta:
        model = TicketMessage

    ticket = factory.SubFactory(SupportTicketFactory)
    sender = factory.LazyAttribute(lambda o: o.ticket.user)
    sender_type = "user"
    text = "Follow-up message"


class ConversationFactory(DjangoModelFactory):
    class Meta:
        model = Conversation

    business = factory.SubFactory(BusinessFactory)
    booker_user = factory.SubFactory(UserFactory)
    booker_contact = None


class ConversationMessageFactory(DjangoModelFactory):
    class Meta:
        model = ConversationMessage

    conversation = factory.SubFactory(ConversationFactory)
    sender_type = ConversationMessage.SENDER_BOOKER
    sender_user = factory.LazyAttribute(lambda o: o.conversation.booker_user)
    text = "Hello from guest"
