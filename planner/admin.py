from django.contrib import admin

from .models import (
    AssetAccount,
    AssumptionReview,
    BackupEvent,
    CashGoal,
    ChangeLogEntry,
    ChildMilestone,
    Debt,
    DepotHolding,
    EquityGrant,
    FamilyGiftPlan,
    FeatureFlag,
    Household,
    ImportBatch,
    IncomeInvestment,
    MoneyMoneyAccountMapping,
    MoneyRule,
    Person,
    PlannedInvestmentPurchase,
    PrivateLoanReceivable,
    RealEstate,
    RealEstateTransferPlan,
    RetirementPlan,
    SalaryChange,
    Scenario,
    Snapshot,
    SnapshotReview,
    SnapshotReviewAction,
    TransferRule,
    TrueExpense,
)
from .feature_flags import environment_override


@admin.register(FeatureFlag)
class FeatureFlagAdmin(admin.ModelAdmin):
    list_display = ["key", "enabled", "effective_enabled", "default_enabled", "updated_at"]
    list_filter = ["enabled", "key"]
    readonly_fields = ["default_enabled", "effective_enabled", "environment_override", "created_at", "updated_at"]
    search_fields = ["key", "description", "notes"]
    fields = [
        "key",
        "enabled",
        "effective_enabled",
        "default_enabled",
        "environment_override",
        "description",
        "notes",
        "created_at",
        "updated_at",
    ]
    actions = ["enable_flags", "disable_flags"]

    @admin.display(boolean=True, description="Effective")
    def effective_enabled(self, obj):
        override = environment_override(obj.key)
        if override is not None:
            return override
        return obj.enabled

    @admin.display(description="Environment override")
    def environment_override(self, obj):
        override = environment_override(obj.key)
        if override is None:
            return "Not set"
        return "Enabled" if override else "Disabled"

    @admin.action(description="Enable selected feature flags")
    def enable_flags(self, request, queryset):
        updated = queryset.update(enabled=True)
        self.message_user(request, f"{updated} feature flag(s) enabled.")

    @admin.action(description="Disable selected feature flags")
    def disable_flags(self, request, queryset):
        updated = queryset.update(enabled=False)
        self.message_user(request, f"{updated} feature flag(s) disabled.")


@admin.register(Household)
class HouseholdAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "data_mode",
        "starting_balance",
        "start_month",
        "planning_months",
        "planning_years",
        "display_granularity",
        "hidden_attention_items",
        "annual_inflation_rate",
        "default_income_growth_rate",
        "default_operating_account",
        "pension_tax_rate",
        "capital_gains_tax_rate",
        "health_insurance_rate",
        "currency",
    ]
    list_filter = ["data_mode", "display_granularity", "currency"]


@admin.register(ImportBatch)
class ImportBatchAdmin(admin.ModelAdmin):
    list_display = ["created_at", "source", "status", "filename", "row_count", "valid_count", "error_count", "household"]
    list_filter = ["source", "status", "created_at"]
    readonly_fields = ["created_at", "summary"]
    search_fields = ["filename", "notes"]


@admin.register(BackupEvent)
class BackupEventAdmin(admin.ModelAdmin):
    list_display = ["created_at", "action", "status", "filename", "pre_restore_filename"]
    list_filter = ["action", "status", "created_at"]
    readonly_fields = ["created_at"]
    search_fields = ["filename", "pre_restore_filename", "detail"]


@admin.register(ChangeLogEntry)
class ChangeLogEntryAdmin(admin.ModelAdmin):
    list_display = ["created_at", "household", "action", "model_name", "object_label"]
    list_filter = ["action", "model_name", "created_at", "household"]
    readonly_fields = ["household", "action", "model_name", "object_pk", "object_label", "changed_fields", "before", "after", "created_at"]
    search_fields = ["object_label", "model_name", "object_pk"]


@admin.register(AssumptionReview)
class AssumptionReviewAdmin(admin.ModelAdmin):
    list_display = ["label", "household", "key", "reviewed_at", "reviewed_by"]
    list_filter = ["reviewed_at", "household"]
    search_fields = ["label", "key", "reviewed_by", "note"]
    readonly_fields = ["reviewed_at"]


@admin.register(MoneyMoneyAccountMapping)
class MoneyMoneyAccountMappingAdmin(admin.ModelAdmin):
    list_display = ["account_name", "source_kind", "source_key", "account_type", "import_enabled", "household", "updated_at"]
    list_filter = ["import_enabled", "source_kind", "account_type", "household"]
    search_fields = ["account_name", "source_key", "notes"]


@admin.register(Snapshot)
class SnapshotAdmin(admin.ModelAdmin):
    list_display = ["name", "household", "snapshot_type", "is_baseline", "snapshot_date", "created_at"]
    list_filter = ["snapshot_type", "is_baseline", "snapshot_date", "created_at"]
    readonly_fields = ["summary", "created_at"]
    search_fields = ["name", "notes"]


@admin.register(SnapshotReview)
class SnapshotReviewAdmin(admin.ModelAdmin):
    list_display = ["title", "household", "baseline_snapshot", "comparison_snapshot", "review_date", "updated_at"]
    list_filter = ["review_date", "created_at"]
    search_fields = ["title", "planned_summary", "actual_summary", "lessons_learned", "next_actions"]


@admin.register(SnapshotReviewAction)
class SnapshotReviewActionAdmin(admin.ModelAdmin):
    list_display = ["title", "review", "owner", "due_date", "status", "updated_at"]
    list_filter = ["status", "due_date"]
    search_fields = ["title", "notes", "review__title"]


@admin.register(Person)
class PersonAdmin(admin.ModelAdmin):
    list_display = ["name", "role", "household", "birth_date", "active_from", "active_until"]
    list_filter = ["role"]


@admin.register(MoneyRule)
class MoneyRuleAdmin(admin.ModelAdmin):
    list_display = ["name", "kind", "amount", "cadence", "annual_growth_rate", "account", "person", "category", "is_active"]
    list_filter = ["kind", "cadence", "account__account_type", "is_active"]


@admin.register(TransferRule)
class TransferRuleAdmin(admin.ModelAdmin):
    list_display = ["name", "source_account", "target_account", "amount", "cadence", "person", "category", "is_active"]
    list_filter = ["source_account__account_type", "target_account__account_type", "cadence", "is_active"]


@admin.register(FamilyGiftPlan)
class FamilyGiftPlanAdmin(admin.ModelAdmin):
    list_display = ["name", "giver", "recipient", "target_account", "amount", "gift_month", "allowance_amount", "is_active"]
    list_filter = ["is_active", "gift_month", "giver", "recipient"]
    search_fields = ["name", "purpose", "notes", "giver__name", "recipient__name", "target_account__name"]


@admin.register(PlannedInvestmentPurchase)
class PlannedInvestmentPurchaseAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "asset_type",
        "source_account",
        "target_account",
        "purchase_amount",
        "purchase_month",
        "payout_date",
        "payout_amount",
        "annual_distribution_rate",
        "distribution_cadence",
        "is_active",
    ]
    list_filter = ["asset_type", "is_active", "target_account__account_type"]
    search_fields = ["name", "isin", "ticker", "notes"]


@admin.register(AssetAccount)
class AssetAccountAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "account_type",
        "owner_type",
        "owner_person",
        "counts_in_household_net_worth",
        "balance",
        "depot_valuation",
        "depot_annual_return_rate",
        "depot_teilfreistellung_rate",
        "depot_vorabpauschale_enabled",
        "currency",
        "source",
        "institution",
        "as_of_date",
    ]
    list_filter = ["account_type", "owner_type", "counts_in_household_net_worth", "source"]
    search_fields = ["name", "institution", "moneymoney_account_key", "ynab_account_id"]


@admin.register(CashGoal)
class CashGoalAdmin(admin.ModelAdmin):
    list_display = ["name", "household", "annual_amount", "start_year", "end_year", "is_active"]
    list_filter = ["is_active", "start_year"]


@admin.register(DepotHolding)
class DepotHoldingAdmin(admin.ModelAdmin):
    list_display = ["name", "asset_account", "isin", "quantity", "latest_price", "currency", "as_of_date", "payout_date", "payout_amount", "annual_distribution_rate", "distribution_cadence"]
    list_filter = ["asset_class", "currency", "distribution_cadence"]


@admin.register(RealEstateTransferPlan)
class RealEstateTransferPlanAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "property_item",
        "giver",
        "recipient",
        "transfer_month",
        "ownership_percent",
        "taxable_gift_value",
        "retained_niessbrauch",
        "is_active",
    ]
    list_filter = ["is_active", "retained_niessbrauch", "transfer_month"]
    search_fields = ["name", "property_item__name", "giver__name", "recipient__name", "notes"]


@admin.register(Debt)
class DebtAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "account",
        "source_account",
        "current_principal",
        "annual_interest_rate",
        "monthly_payment",
        "fixed_interest_until",
        "refinance_annual_interest_rate",
        "refinance_monthly_payment",
        "is_active",
    ]
    list_filter = ["is_active", "source_account__account_type"]


@admin.register(IncomeInvestment)
class IncomeInvestmentAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "investment_type",
        "principal",
        "monthly_income",
        "annual_growth_rate",
        "start_month",
        "end_month",
        "is_active",
    ]
    list_filter = ["investment_type", "is_active"]


@admin.register(PrivateLoanReceivable)
class PrivateLoanReceivableAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "borrower",
        "source_account",
        "current_principal",
        "annual_interest_rate",
        "interest_tax_rate",
        "monthly_interest_income",
        "monthly_principal_repayment",
        "start_month",
        "end_month",
        "is_active",
    ]
    list_filter = ["is_active", "start_month", "source_account__account_type"]


@admin.register(RealEstate)
class RealEstateAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "use",
        "current_value",
        "annual_appreciation_rate",
        "monthly_costs",
        "saved_monthly_rent",
        "monthly_rent",
        "acquisition_month",
        "sale_month",
        "is_active",
    ]
    list_filter = ["use", "is_active", "source_account__account_type"]
    search_fields = ["name", "notes"]


@admin.register(RetirementPlan)
class RetirementPlanAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "person",
        "vehicle_type",
        "current_pension_points",
        "expected_annual_points",
        "private_monthly_pension",
        "pension_value_per_point",
        "retirement_start_month",
        "is_active",
    ]
    list_filter = ["vehicle_type", "is_active"]


@admin.register(EquityGrant)
class EquityGrantAdmin(admin.ModelAdmin):
    list_display = [
        "name",
        "person",
        "account",
        "grant_type",
        "gross_vest_value",
        "withholding_rate",
        "cadence",
        "first_vest_month",
        "last_vest_month",
        "is_active",
    ]
    list_filter = ["grant_type", "cadence", "account__account_type", "is_active"]


@admin.register(Scenario)
class ScenarioAdmin(admin.ModelAdmin):
    list_display = ["name", "household", "liquid_balance_delta", "monthly_income_delta", "monthly_expense_delta", "is_active"]
    list_filter = ["is_active"]


@admin.register(TrueExpense)
class TrueExpenseAdmin(admin.ModelAdmin):
    list_display = ["name", "household", "category", "account", "amount", "cadence", "first_due_month", "end_month", "is_active"]
    list_filter = ["cadence", "account__account_type", "is_active"]


@admin.register(ChildMilestone)
class ChildMilestoneAdmin(admin.ModelAdmin):
    list_display = ["name", "person", "start_month", "end_month", "monthly_cost_delta", "monthly_income_delta", "is_active"]
    list_filter = ["is_active"]


@admin.register(SalaryChange)
class SalaryChangeAdmin(admin.ModelAdmin):
    list_display = ["name", "person", "account", "start_month", "end_month", "monthly_net_income_delta", "is_active"]
    list_filter = ["account__account_type", "is_active"]

# Register your models here.
