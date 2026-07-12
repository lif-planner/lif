import threading
from contextlib import contextmanager

from django.core.exceptions import ObjectDoesNotExist
from django.core.serializers.json import DjangoJSONEncoder
from django.db.models.signals import post_delete, post_migrate, post_save, pre_save
from django.dispatch import receiver
from django.forms.models import model_to_dict

from .feature_flags import FEATURE_FLAG_DEFINITIONS

CHANGE_LOG_MODELS = {}
IGNORED_CHANGE_FIELDS = {"created_at", "updated_at", "hidden_attention_items"}

_local = threading.local()


def _suspended_household_ids():
    ids = getattr(_local, "household_ids", None)
    if ids is None:
        ids = set()
        _local.household_ids = ids
    return ids


@contextmanager
def suspend_change_log_for_households(household_ids):
    """Skip change-log writes for these households while the block runs.

    Deleting a household cascades to its tracked child rows, and each child
    delete would otherwise insert a new ChangeLogEntry referencing the
    household. Those inserts happen after Django's delete collector already
    took its snapshot, so they are not cleaned up when the household row
    itself is removed moments later, leaving a dangling foreign key.
    """
    ids = _suspended_household_ids()
    to_add = [household_id for household_id in household_ids if household_id is not None]
    ids.update(to_add)
    try:
        yield
    finally:
        for household_id in to_add:
            ids.discard(household_id)


def is_household_change_log_suspended(household_id):
    return household_id in _suspended_household_ids()


def _planning_models():
    from .models import (
        AssetAccount,
        CashGoal,
        ChildMilestone,
        Debt,
        DepotHolding,
        EquityGrant,
        FamilyGiftPlan,
        Household,
        IncomeInvestment,
        MoneyRule,
        Person,
        PlannedInvestmentPurchase,
        PrivateLoanReceivable,
        RealEstate,
        RealEstateTransferPlan,
        RetirementPlan,
        SalaryChange,
        TransferRule,
        TrueExpense,
    )

    return {
        Household,
        AssetAccount,
        CashGoal,
        ChildMilestone,
        Debt,
        DepotHolding,
        EquityGrant,
        FamilyGiftPlan,
        IncomeInvestment,
        MoneyRule,
        Person,
        PlannedInvestmentPurchase,
        PrivateLoanReceivable,
        RealEstate,
        RealEstateTransferPlan,
        RetirementPlan,
        SalaryChange,
        TransferRule,
        TrueExpense,
    }


def _is_tracked_model(model):
    return model in _planning_models()


def _jsonable(value):
    return DjangoJSONEncoder().default(value) if not isinstance(value, (str, int, float, bool, list, dict, type(None))) else value


def _snapshot(instance):
    data = model_to_dict(instance)
    return {key: _jsonable(value) for key, value in data.items()}


def _household_for(instance):
    try:
        if instance.__class__.__name__ == "Household":
            return instance
        if hasattr(instance, "household_id"):
            return instance.household
        if hasattr(instance, "person_id") and instance.person_id:
            return instance.person.household
        if hasattr(instance, "asset_account_id") and instance.asset_account_id:
            return instance.asset_account.household
    except ObjectDoesNotExist:
        return None
    return None


def _object_label(instance):
    try:
        return str(instance)
    except Exception:
        return instance.__class__.__name__


@receiver(post_migrate)
def ensure_feature_flags(sender, **kwargs):
    if sender.name != "planner":
        return

    from .models import FeatureFlag

    for key, definition in FEATURE_FLAG_DEFINITIONS.items():
        FeatureFlag.objects.get_or_create(
            key=key,
            defaults={
                "enabled": definition["default"],
                "description": definition["description"],
            },
        )


@receiver(pre_save)
def capture_change_log_before(sender, instance, **kwargs):
    if not _is_tracked_model(sender) or not instance.pk:
        return
    try:
        old_instance = sender.objects.get(pk=instance.pk)
    except sender.DoesNotExist:
        return
    instance._change_log_before = _snapshot(old_instance)


@receiver(post_save)
def write_change_log_after_save(sender, instance, created, **kwargs):
    if not _is_tracked_model(sender):
        return
    household = _household_for(instance)
    if household is not None and is_household_change_log_suspended(household.pk):
        return
    from .models import ChangeLogEntry

    after = _snapshot(instance)
    before = {} if created else getattr(instance, "_change_log_before", {})
    changed_fields = sorted(
        field
        for field in set(before) | set(after)
        if field not in IGNORED_CHANGE_FIELDS and before.get(field) != after.get(field)
    )
    if not created and not changed_fields:
        return
    ChangeLogEntry.objects.create(
        household=household,
        action=ChangeLogEntry.Action.CREATED if created else ChangeLogEntry.Action.UPDATED,
        model_name=sender.__name__,
        object_pk=str(instance.pk),
        object_label=_object_label(instance),
        changed_fields=changed_fields,
        before=before,
        after=after,
    )


@receiver(post_delete)
def write_change_log_after_delete(sender, instance, **kwargs):
    if not _is_tracked_model(sender):
        return
    household = _household_for(instance)
    if household is not None and is_household_change_log_suspended(household.pk):
        return
    from .models import ChangeLogEntry

    before = _snapshot(instance)
    ChangeLogEntry.objects.create(
        household=household,
        action=ChangeLogEntry.Action.DELETED,
        model_name=sender.__name__,
        object_pk=str(instance.pk),
        object_label=_object_label(instance),
        changed_fields=sorted(field for field in before if field not in IGNORED_CHANGE_FIELDS),
        before=before,
        after={},
    )
