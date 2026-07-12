from django.db import transaction

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
    Scenario,
    TransferRule,
    TrueExpense,
)
from .signals import suspend_change_log_for_households

# moneymoney_account_key/ynab_account_id identify a real connector-linked bank
# account; carrying them into a clone would let a later live import silently
# overwrite the clone's balance from the source household's real bank feed.
COPY_EXCLUDE = {"id", "created_at", "updated_at", "moneymoney_account_key", "ynab_account_id"}


def _copy_field_values(instance, exclude=()):
    excluded = COPY_EXCLUDE | set(exclude)
    values = {}
    for field in instance._meta.fields:
        if field.name in excluded or field.primary_key:
            continue
        values[field.attname] = getattr(instance, field.attname)
    return values


def _copy_instance(instance, **overrides):
    values = _copy_field_values(instance, exclude=overrides.keys())
    values.update(overrides)
    return instance.__class__.objects.create(**values)


def _clone_queryset(queryset, **overrides):
    mapping = {}
    for item in queryset:
        mapping[item.pk] = _copy_instance(item, **overrides)
    return mapping


def _mapped(mapping, value):
    if value is None:
        return None
    return mapping.get(value)


@transaction.atomic
def clone_household(source_household, name=None, make_active=False):
    """Clone planning data into a new household for structural what-if plans.

    This intentionally copies assumptions, people, accounts, and planning rules,
    but not operational history such as imports, snapshots, reviews, or connector
    mappings. Those describe how the source household was maintained, not the
    future plan being compared.
    """
    household_values = _copy_field_values(
        source_household,
        exclude={"name", "is_active", "default_operating_account", "hidden_attention_items"},
    )
    clone = Household.objects.create(
        **household_values,
        name=name or f"{source_household.name} copy",
        is_active=False,
        hidden_attention_items={},
    )

    # Cloning copies dozens of rows in one request; without this, each copied
    # row would fire the change-log signal and flood the brand-new household's
    # history with clone-time noise indistinguishable from real edits.
    with suspend_change_log_for_households([clone.pk]):
        person_map = _clone_queryset(source_household.people.all(), household=clone)
        account_map = _clone_queryset(source_household.accounts.all(), household=clone)

        if source_household.default_operating_account_id:
            clone.default_operating_account = _mapped(account_map, source_household.default_operating_account_id)
            clone.save(update_fields=["default_operating_account", "updated_at"])

        for account in source_household.accounts.prefetch_related("holdings"):
            cloned_account = account_map[account.pk]
            for holding in account.holdings.all():
                _copy_instance(holding, asset_account=cloned_account)

        _clone_queryset(source_household.cash_goals.all(), household=clone)
        _clone_queryset(source_household.scenarios.all(), household=clone)

        property_map = {}
        for item in source_household.properties.all():
            property_map[item.pk] = _copy_instance(
                item,
                household=clone,
                source_account=_mapped(account_map, item.source_account_id),
                sale_proceeds_account=_mapped(account_map, item.sale_proceeds_account_id),
            )

        for debt in source_household.debts.all():
            _copy_instance(
                debt,
                household=clone,
                account=account_map[debt.account_id],
                source_account=_mapped(account_map, debt.source_account_id),
                real_estate=_mapped(property_map, debt.real_estate_id),
            )

        for item in source_household.income_investments.all():
            _copy_instance(
                item,
                household=clone,
                source_account=_mapped(account_map, item.source_account_id),
            )

        for item in source_household.private_loans.all():
            _copy_instance(
                item,
                household=clone,
                source_account=_mapped(account_map, item.source_account_id),
            )

        for item in source_household.retirement_plans.all():
            _copy_instance(
                item,
                household=clone,
                person=person_map[item.person_id],
            )

        for item in source_household.equity_grants.all():
            _copy_instance(
                item,
                household=clone,
                person=person_map[item.person_id],
                account=_mapped(account_map, item.account_id),
            )

        for item in source_household.true_expenses.all():
            _copy_instance(
                item,
                household=clone,
                account=_mapped(account_map, item.account_id),
            )

        for person in source_household.people.prefetch_related("child_milestones", "salary_changes"):
            cloned_person = person_map[person.pk]
            for item in person.child_milestones.all():
                _copy_instance(item, person=cloned_person)
            for item in person.salary_changes.all():
                _copy_instance(
                    item,
                    person=cloned_person,
                    account=_mapped(account_map, item.account_id),
                )

        for rule in source_household.rules.all():
            _copy_instance(
                rule,
                household=clone,
                person=_mapped(person_map, rule.person_id),
                account=_mapped(account_map, rule.account_id),
            )

        for rule in source_household.transfer_rules.all():
            _copy_instance(
                rule,
                household=clone,
                person=_mapped(person_map, rule.person_id),
                source_account=_mapped(account_map, rule.source_account_id),
                target_account=account_map[rule.target_account_id],
            )

        for gift in source_household.family_gift_plans.all():
            _copy_instance(
                gift,
                household=clone,
                giver=person_map[gift.giver_id],
                recipient=person_map[gift.recipient_id],
                source_account=_mapped(account_map, gift.source_account_id),
                target_account=account_map[gift.target_account_id],
            )

        for transfer in source_household.real_estate_transfer_plans.all():
            _copy_instance(
                transfer,
                household=clone,
                property_item=property_map[transfer.property_item_id],
                giver=person_map[transfer.giver_id],
                recipient=person_map[transfer.recipient_id],
            )

        for purchase in source_household.planned_investment_purchases.all():
            _copy_instance(
                purchase,
                household=clone,
                person=_mapped(person_map, purchase.person_id),
                source_account=_mapped(account_map, purchase.source_account_id),
                target_account=account_map[purchase.target_account_id],
            )

        if make_active:
            clone.is_active = True
            clone.save(update_fields=["is_active", "updated_at"])

    return clone
