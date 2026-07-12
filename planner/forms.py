from datetime import date
from decimal import Decimal

from django import forms

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
    Snapshot,
    SnapshotReview,
    SnapshotReviewAction,
    TransferRule,
    TrueExpense,
)


def validate_household_currency(value, household):
    """Normalise a currency code and enforce the single-currency invariant: the
    projection sums balances without FX conversion, so every account and holding
    must use the household currency."""
    currency = (value or "").strip().upper()
    if len(currency) != 3:
        raise forms.ValidationError("Use a three-letter currency code.")
    if household and currency != household.currency:
        raise forms.ValidationError(
            f"Use the household currency ({household.currency}). Multi-currency planning is "
            "not supported — the projection does not convert between currencies."
        )
    return currency


class DateInput(forms.DateInput):
    input_type = "date"


class MonthInput(forms.DateInput):
    input_type = "month"

    def __init__(self, attrs=None, format=None):
        # Safari has no native <input type="month"> picker and falls back to a
        # plain text box, so the expected YYYY-MM format needs to be visible
        # even without a calendar UI.
        attrs = {"placeholder": "YYYY-MM", **(attrs or {})}
        super().__init__(attrs=attrs, format=format or "%Y-%m")


class AccountBalanceSelect(forms.Select):
    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex=subindex, attrs=attrs)
        account = getattr(value, "instance", None)
        if account:
            option["attrs"]["data-balance"] = str(account.balance)
            option["attrs"]["data-principal"] = str(abs(account.balance))
            option["attrs"]["data-currency"] = account.currency
        return option


class LoanAccountChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, account):
        return f"{account.name} - {account.balance} {account.currency} (principal {abs(account.balance)} {account.currency})"


class AccountChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, account):
        return f"{account.name} ({account.effective_balance:,.2f} {account.currency})"


class GoalPlannerForm(forms.Form):
    target_net_worth = forms.DecimalField(
        max_digits=14,
        decimal_places=2,
        min_value=0,
        initial=Decimal("1000000.00"),
        help_text="Net-worth target to reach by the selected year.",
    )
    target_year = forms.IntegerField(
        min_value=1900,
        max_value=2200,
        help_text="The solver checks the forecast at the end of this year.",
    )
    start_month = forms.DateField(
        required=False,
        widget=MonthInput,
        input_formats=["%Y-%m"],
        help_text="Month when the extra monthly surplus starts. Leave empty to start with the household forecast.",
    )
    target_account = AccountChoiceField(
        queryset=AssetAccount.objects.none(),
        required=False,
        help_text="Optional depot or liquid account receiving the extra monthly surplus. A depot lets the forecast apply its growth assumption.",
    )

    def __init__(self, *args, household=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.household = household
        if household:
            self.fields["target_year"].initial = household.start_month.year + max(household.projection_months // 12, 1)
            self.fields["start_month"].initial = household.start_month
            self.fields["target_account"].queryset = household.accounts.filter(
                account_type__in=[
                    AssetAccount.AccountType.DEPOT,
                    AssetAccount.AccountType.CASH,
                    AssetAccount.AccountType.SAVINGS,
                ]
            ).order_by("account_type", "name", "id")

    def clean_target_year(self):
        target_year = self.cleaned_data["target_year"]
        if self.household:
            last_year = add_months(self.household.start_month, self.household.projection_months - 1).year
            if target_year < self.household.start_month.year:
                raise forms.ValidationError("Choose a year inside the household forecast.")
            if target_year > last_year:
                raise forms.ValidationError(f"Choose a year inside the current forecast horizon, up to {last_year}.")
        return target_year

    def clean_start_month(self):
        start_month = self.cleaned_data.get("start_month")
        if start_month and self.household:
            start_month = date(start_month.year, start_month.month, 1)
            if start_month < date(self.household.start_month.year, self.household.start_month.month, 1):
                raise forms.ValidationError("Start inside the household forecast horizon.")
        return start_month


def add_months(value, months):
    year = value.year + ((value.month - 1 + months) // 12)
    month = ((value.month - 1 + months) % 12) + 1
    return date(year, month, 1)


def first_of_month(value):
    return date(value.year, value.month, 1)


def cash_flow_accounts(household):
    if not household:
        return AssetAccount.objects.none()
    return household.accounts.filter(account_type__in=[AssetAccount.AccountType.CASH, AssetAccount.AccountType.SAVINGS])


class ScenarioHouseholdCloneForm(forms.Form):
    name = forms.CharField(
        max_length=120,
        label="Scenario household name",
        help_text="Example: Scenario: buy bigger house, retire earlier, or conservative returns.",
    )


class AccountCsvImportForm(forms.Form):
    csv_file = forms.FileField(
        label="Accounts CSV",
        help_text="Expected columns: name, account_type, balance, currency, institution, as_of_date.",
    )


class DepotHoldingCsvImportForm(forms.Form):
    csv_file = forms.FileField(
        label="Depot holdings CSV",
        help_text=(
            "Expected columns: account_name, name, isin, ticker, asset_class, quantity, "
            "latest_price, currency, as_of_date, payout_date. Optional: payout_amount."
        ),
    )


class MoneyMoneyMappingAddForm(forms.Form):
    source_key = forms.CharField(
        max_length=220,
        required=False,
        label="MoneyMoney source key",
        help_text="Optional before preview. If blank, LiF uses a legacy key from the account name.",
    )
    account_name = forms.CharField(max_length=140, label="MoneyMoney account name")
    account_type = forms.ChoiceField(
        choices=[("", "Use import default"), *AssetAccount.AccountType.choices],
        required=False,
        label="LiF account type",
    )
    import_enabled = forms.BooleanField(required=False, initial=True, label="Import or sync this account")
    notes = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))

    def clean(self):
        cleaned_data = super().clean()
        source_key = (cleaned_data.get("source_key") or "").strip()
        account_name = (cleaned_data.get("account_name") or "").strip()
        if not source_key and account_name:
            cleaned_data["source_key"] = f"legacy-name:{account_name}"
        return cleaned_data


class FirstRunSetupForm(forms.Form):
    household_name = forms.CharField(max_length=120, initial="Home")
    currency = forms.CharField(max_length=3, initial="EUR")
    start_month = forms.DateField(widget=DateInput())
    planning_years = forms.IntegerField(min_value=1, max_value=80, initial=40)
    annual_cash_goal = forms.DecimalField(max_digits=12, decimal_places=2, initial="30000.00")

    adult_1_name = forms.CharField(max_length=120, initial="Parent 1")
    adult_1_birth_date = forms.DateField(required=False, widget=DateInput())
    adult_1_monthly_salary = forms.DecimalField(max_digits=12, decimal_places=2, initial="3200.00")

    adult_2_name = forms.CharField(max_length=120, initial="Parent 2")
    adult_2_birth_date = forms.DateField(required=False, widget=DateInput())
    adult_2_monthly_salary = forms.DecimalField(max_digits=12, decimal_places=2, initial="2400.00")

    child_1_name = forms.CharField(max_length=120, initial="Child 1")
    child_1_birth_date = forms.DateField(required=False, widget=DateInput())
    child_1_kindergeld = forms.DecimalField(max_digits=12, decimal_places=2, initial="255.00")

    child_2_name = forms.CharField(max_length=120, initial="Child 2")
    child_2_birth_date = forms.DateField(required=False, widget=DateInput())
    child_2_kindergeld = forms.DecimalField(max_digits=12, decimal_places=2, initial="255.00")

    def clean_currency(self):
        currency = self.cleaned_data["currency"].strip().upper()
        if len(currency) != 3:
            raise forms.ValidationError("Use a three-letter currency code.")
        return currency


class HouseholdForm(forms.ModelForm):
    class Meta:
        model = Household
        fields = [
            "name",
            "data_mode",
            "starting_balance",
            "start_month",
            "planning_months",
            "planning_years",
            "display_granularity",
            "annual_inflation_rate",
            "default_income_growth_rate",
            "default_operating_account",
            "pension_tax_rate",
            "income_tax_rate",
            "capital_gains_tax_rate",
            "capital_income_allowance",
            "vorabpauschale_basiszins_rate",
            "church_tax_rate",
            "solidarity_surcharge_rate",
            "health_insurance_rate",
            "fund_cash_goal_from_depot",
            "emergency_fund_months",
            "currency",
        ]
        widgets = {"start_month": DateInput()}

    def __init__(self, *args, household=None, **kwargs):
        super().__init__(*args, **kwargs)
        current_household = household or self.instance
        if current_household and getattr(current_household, "pk", None):
            self.fields["default_operating_account"].queryset = cash_flow_accounts(current_household)
        else:
            self.fields["default_operating_account"].queryset = AssetAccount.objects.none()
        self.fields["default_operating_account"].required = False
        self.fields["default_operating_account"].label = "Default operating account"
        self.fields["default_operating_account"].help_text = (
            "Cash or savings account used by income and expense rules when the rule does not choose an account."
        )
        self.fields["emergency_fund_months"].label = "Emergency fund target (months)"
        self.fields["emergency_fund_months"].help_text = (
            "Notfallgroschen target in months of recurring expenses. Set 0 to disable the check."
        )


class SnapshotForm(forms.ModelForm):
    class Meta:
        model = Snapshot
        fields = ["name", "snapshot_type", "is_baseline", "snapshot_date", "notes"]
        widgets = {"snapshot_date": DateInput()}

    def clean(self):
        cleaned_data = super().clean()
        # A snapshot pinned as the baseline should always carry the baseline
        # type, and vice versa -- otherwise the type badge and the actual
        # "is this the baseline" behavior can visibly disagree.
        if cleaned_data.get("is_baseline") or cleaned_data.get("snapshot_type") == Snapshot.SnapshotType.BASELINE:
            cleaned_data["is_baseline"] = True
            cleaned_data["snapshot_type"] = Snapshot.SnapshotType.BASELINE
        return cleaned_data


class SnapshotReviewForm(forms.ModelForm):
    class Meta:
        model = SnapshotReview
        fields = ["title", "review_date", "planned_summary", "actual_summary", "lessons_learned", "next_actions"]
        widgets = {"review_date": DateInput()}


class SnapshotReviewActionForm(forms.ModelForm):
    class Meta:
        model = SnapshotReviewAction
        fields = ["title", "owner", "due_date", "status", "notes"]
        widgets = {"due_date": DateInput()}

    def __init__(self, *args, household=None, **kwargs):
        super().__init__(*args, **kwargs)
        if household:
            self.fields["owner"].queryset = household.people.all()
        self.fields["owner"].required = False
        self.fields["due_date"].required = False


class PersonForm(forms.ModelForm):
    class Meta:
        model = Person
        fields = ["name", "role", "birth_date", "active_from", "active_until", "notes"]
        widgets = {
            "birth_date": DateInput(),
            "active_from": DateInput(),
            "active_until": DateInput(),
        }


class MoneyRuleForm(forms.ModelForm):
    class Meta:
        model = MoneyRule
        fields = [
            "name",
            "kind",
            "amount",
            "cadence",
            "annual_growth_rate",
            "person",
            "account",
            "category",
            "is_taxable",
            "start_month",
            "end_month",
            "is_active",
            "notes",
        ]
        widgets = {
            "start_month": DateInput(),
            "end_month": DateInput(),
        }

    def __init__(self, *args, household=None, **kwargs):
        super().__init__(*args, **kwargs)
        if household:
            self.fields["person"].queryset = household.people.all()
            self.fields["account"].queryset = cash_flow_accounts(household)
        else:
            self.fields["account"].queryset = AssetAccount.objects.none()
        self.fields["person"].required = False
        self.fields["account"].required = False
        self.fields["account"].label = "Cash-flow account"
        self.fields["account"].help_text = (
            "Optional. Income lands here and expenses are paid from here. Leave blank to use the household default operating account."
        )
        self.fields["annual_growth_rate"].label = "Annual growth override"
        self.fields["annual_growth_rate"].help_text = (
            "Leave blank to use the household default income growth. Enter 0.00 to keep this rule flat."
        )


class TransferRuleForm(forms.ModelForm):
    class Meta:
        model = TransferRule
        fields = [
            "name",
            "source_account",
            "target_account",
            "amount",
            "cadence",
            "person",
            "category",
            "start_month",
            "end_month",
            "is_active",
            "notes",
        ]
        widgets = {
            "start_month": DateInput(),
            "end_month": DateInput(),
        }

    def __init__(self, *args, household=None, **kwargs):
        super().__init__(*args, **kwargs)
        if household:
            self.fields["person"].queryset = household.people.all()
            self.fields["source_account"].queryset = cash_flow_accounts(household)
            self.fields["target_account"].queryset = household.accounts.all()
        self.fields["person"].required = False
        self.fields["source_account"].required = False
        self.fields["source_account"].label = "Source account"
        self.fields["source_account"].help_text = (
            "Optional. Pick a cash or savings account to fund this transfer; leave blank to use the general liquid pool."
        )
        self.fields["target_account"].label = "Target account"
        self.fields["cadence"].help_text = "Monthly and yearly repeat. Once applies the full amount in the start month only."
        self.fields["start_month"].help_text = (
            "Required for one-time transfers. For yearly transfers this is also the yearly anchor month."
        )

    def clean(self):
        cleaned_data = super().clean()
        source_account = cleaned_data.get("source_account")
        target_account = cleaned_data.get("target_account")
        cadence = cleaned_data.get("cadence")
        start_month = cleaned_data.get("start_month")
        if source_account and target_account and source_account == target_account:
            self.add_error("target_account", "Source and target accounts must be different.")
        if cadence == TransferRule.Cadence.ONCE and not start_month:
            self.add_error("start_month", "Set the month when this one-time transfer should happen.")
        return cleaned_data


class PlannedInvestmentPurchaseForm(forms.ModelForm):
    class Meta:
        model = PlannedInvestmentPurchase
        fields = [
            "name",
            "asset_type",
            "isin",
            "ticker",
            "source_account",
            "target_account",
            "purchase_amount",
            "purchase_month",
            "payout_date",
            "payout_amount",
            "annual_distribution_rate",
            "distribution_cadence",
            "person",
            "is_active",
            "notes",
        ]
        widgets = {
            "purchase_month": MonthInput(),
            "payout_date": DateInput(),
        }

    def __init__(self, *args, household=None, **kwargs):
        super().__init__(*args, **kwargs)
        if household:
            self.fields["person"].queryset = household.people.all()
            self.fields["source_account"].queryset = cash_flow_accounts(household)
            self.fields["target_account"].queryset = household.accounts.filter(account_type=AssetAccount.AccountType.DEPOT)
        self.fields["person"].required = False
        self.fields["source_account"].required = False
        self.fields["source_account"].label = "Funding account"
        self.fields["source_account"].help_text = (
            "Optional. Pick the cash or savings account that will pay for the purchase; leave blank to use the general liquid pool."
        )
        self.fields["target_account"].label = "Depot account"
        self.fields["purchase_amount"].help_text = (
            "Cash invested on the purchase month. The same amount is added to the depot value in the forecast."
        )
        self.fields["purchase_month"].input_formats = ["%Y-%m", "%Y-%m-%d"]
        self.fields["purchase_month"].help_text = "The purchase is applied once in this month."
        self.fields["annual_distribution_rate"].help_text = (
            "Recurring distribution yield for this specific purchase, e.g. a distributing ETF or dividend "
            "stock. Keep 0 for accumulating funds. Only applies when the target depot is valued by summed "
            "holdings; a flat-balance depot already covers this via its own distribution rate."
        )
        self.fields["payout_date"].help_text = (
            "Optional. For bonds, set the maturity date so the forecast moves the payout back to cash."
        )
        self.fields["payout_amount"].help_text = (
            "Optional expected cash at maturity. For example, a bond bought for 28,450 EUR may pay 30,000 EUR."
        )

    def clean(self):
        cleaned_data = super().clean()
        source_account = cleaned_data.get("source_account")
        target_account = cleaned_data.get("target_account")
        purchase_month = cleaned_data.get("purchase_month")
        payout_date = cleaned_data.get("payout_date")
        payout_amount = cleaned_data.get("payout_amount")
        if source_account and target_account and source_account == target_account:
            self.add_error("target_account", "Funding and depot account must be different.")
        if target_account and target_account.account_type != AssetAccount.AccountType.DEPOT:
            self.add_error("target_account", "Choose a depot account as the purchase target.")
        if payout_amount and not payout_date:
            self.add_error("payout_date", "Set a payout date when entering a payout amount.")
        if purchase_month and payout_date and first_of_month(payout_date) < first_of_month(purchase_month):
            self.add_error("payout_date", "Payout date must be on or after the purchase month.")
        return cleaned_data


class AssetAccountForm(forms.ModelForm):
    class Meta:
        model = AssetAccount
        fields = [
            "name",
            "account_type",
            "owner_type",
            "owner_person",
            "counts_in_household_net_worth",
            "balance",
            "currency",
            "source",
            "depot_valuation",
            "depot_annual_return_rate",
            "depot_annual_distribution_rate",
            "depot_teilfreistellung_rate",
            "depot_vorabpauschale_enabled",
            "depot_distribution_cadence",
            "savings_annual_interest_rate",
            "savings_interest_cadence",
            "savings_interest_tax_rate",
            "institution",
            "as_of_date",
            "ynab_account_id",
            "notes",
        ]
        widgets = {"as_of_date": DateInput()}

    def __init__(self, *args, household=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.household = household
        if household:
            self.fields["owner_person"].queryset = household.people.all()
        self.fields["counts_in_household_net_worth"].help_text = (
            "Keep enabled for parent/household assets. Disable for child-owned accounts that you want to track separately."
        )

    def clean_currency(self):
        return validate_household_currency(self.cleaned_data.get("currency"), self.household)

    def clean(self):
        cleaned_data = super().clean()
        owner_type = cleaned_data.get("owner_type")
        owner_person = cleaned_data.get("owner_person")
        if owner_type == AssetAccount.OwnerType.PERSON and not owner_person:
            self.add_error("owner_person", "Choose the person who owns this account.")
        if owner_type != AssetAccount.OwnerType.PERSON:
            cleaned_data["owner_person"] = None
        return cleaned_data


class FamilyGiftPlanForm(forms.ModelForm):
    class Meta:
        model = FamilyGiftPlan
        fields = [
            "name",
            "giver",
            "recipient",
            "source_account",
            "target_account",
            "amount",
            "gift_month",
            "allowance_amount",
            "allowance_window_years",
            "purpose",
            "notes",
            "is_active",
        ]
        widgets = {"gift_month": MonthInput()}

    def __init__(self, *args, household=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.household = household
        if household:
            self.fields["giver"].queryset = household.people.all()
            self.fields["recipient"].queryset = household.people.all()
            self.fields["source_account"].queryset = household.accounts.filter(
                account_type__in=[AssetAccount.AccountType.CASH, AssetAccount.AccountType.SAVINGS]
            )
            self.fields["target_account"].queryset = household.accounts.filter(counts_in_household_net_worth=False)
        self.fields["gift_month"].input_formats = ["%Y-%m", "%Y-%m-%d"]
        self.fields["amount"].help_text = "Cash amount gifted once in the selected month."
        self.fields["target_account"].help_text = (
            "Use a child-owned account that is tracked separately from household planning net worth."
        )

    def clean(self):
        cleaned_data = super().clean()
        giver = cleaned_data.get("giver")
        recipient = cleaned_data.get("recipient")
        source_account = cleaned_data.get("source_account")
        target_account = cleaned_data.get("target_account")
        if giver and recipient and giver == recipient:
            self.add_error("recipient", "Giver and recipient must be different people.")
        if source_account and target_account and source_account == target_account:
            self.add_error("target_account", "Source and target account must be different.")
        if target_account and target_account.counts_in_household_net_worth:
            self.add_error("target_account", "Choose an account that is tracked outside household net worth.")
        return cleaned_data


class AccountSetupWizardForm(forms.Form):
    account_type = forms.ChoiceField(
        choices=AssetAccount.AccountType.choices,
        initial=AssetAccount.AccountType.CASH,
        help_text="Choose the account type first. Only matching sections below are used.",
    )
    name = forms.CharField(max_length=140)
    balance = forms.DecimalField(max_digits=12, decimal_places=2, initial="0.00")
    currency = forms.CharField(max_length=3, initial="EUR")
    institution = forms.CharField(max_length=120, required=False)
    as_of_date = forms.DateField(required=False, widget=DateInput())
    notes = forms.CharField(required=False, widget=forms.Textarea)

    savings_annual_interest_rate = forms.DecimalField(max_digits=5, decimal_places=2, required=False, initial="0.00")
    savings_interest_cadence = forms.ChoiceField(
        choices=AssetAccount.InterestCadence.choices,
        required=False,
        initial=AssetAccount.InterestCadence.MONTHLY,
    )
    savings_interest_tax_rate = forms.DecimalField(max_digits=5, decimal_places=2, required=False, initial="25.00")

    depot_valuation = forms.ChoiceField(
        choices=AssetAccount.DepotValuation.choices,
        required=False,
        initial=AssetAccount.DepotValuation.ACCOUNT_BALANCE,
    )
    depot_annual_return_rate = forms.DecimalField(
        max_digits=5,
        decimal_places=2,
        required=False,
        initial="0.00",
        help_text="Expected nominal annual depot growth in percent. Keep 0 for a flat projection.",
    )
    depot_teilfreistellung_rate = forms.DecimalField(
        max_digits=5,
        decimal_places=2,
        required=False,
        initial="30.00",
        help_text="German partial tax exemption for depot gains/distributions. 30% is common for equity ETFs; use 0% for bonds.",
    )
    depot_vorabpauschale_enabled = forms.BooleanField(
        required=False,
        initial=False,
        help_text="Enable for accumulating funds where German Vorabpauschale should be modeled.",
    )
    holding_name = forms.CharField(max_length=180, required=False)
    holding_isin = forms.CharField(max_length=20, required=False)
    holding_ticker = forms.CharField(max_length=30, required=False)
    holding_asset_class = forms.CharField(max_length=80, required=False, initial="ETF")
    holding_quantity = forms.DecimalField(max_digits=14, decimal_places=6, required=False)
    holding_latest_price = forms.DecimalField(max_digits=12, decimal_places=2, required=False)
    holding_payout_date = forms.DateField(required=False, widget=DateInput())
    holding_payout_amount = forms.DecimalField(max_digits=12, decimal_places=2, required=False)

    debt_name = forms.CharField(max_length=140, required=False)
    debt_annual_interest_rate = forms.DecimalField(max_digits=5, decimal_places=2, required=False)
    debt_monthly_payment = forms.DecimalField(max_digits=12, decimal_places=2, required=False)
    debt_start_month = forms.DateField(required=False, widget=DateInput())
    debt_end_month = forms.DateField(required=False, widget=DateInput())
    debt_fixed_interest_until = forms.DateField(required=False, widget=DateInput())
    debt_refinance_annual_interest_rate = forms.DecimalField(max_digits=5, decimal_places=2, required=False)
    debt_refinance_monthly_payment = forms.DecimalField(max_digits=12, decimal_places=2, required=False)

    def __init__(self, *args, household=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.household = household

    def clean_currency(self):
        return validate_household_currency(self.cleaned_data.get("currency"), self.household)

    def clean(self):
        cleaned_data = super().clean()
        account_type = cleaned_data.get("account_type")
        if account_type == AssetAccount.AccountType.DEPOT:
            holding_values = [
                cleaned_data.get("holding_name"),
                cleaned_data.get("holding_isin"),
                cleaned_data.get("holding_ticker"),
                cleaned_data.get("holding_quantity"),
                cleaned_data.get("holding_latest_price"),
            ]
            if any(value not in {"", None} for value in holding_values):
                for field in ["holding_name", "holding_quantity", "holding_latest_price"]:
                    if cleaned_data.get(field) in {"", None}:
                        self.add_error(field, "Required when adding the first holding.")
        if account_type == AssetAccount.AccountType.LOAN:
            for field in ["debt_annual_interest_rate", "debt_monthly_payment"]:
                if cleaned_data.get(field) in {"", None}:
                    self.add_error(field, "Required for mortgage or loan setup.")
        return cleaned_data


class CashGoalForm(forms.ModelForm):
    class Meta:
        model = CashGoal
        fields = ["name", "annual_amount", "indexed_to_inflation", "start_year", "end_year", "is_active", "notes"]


class DepotHoldingForm(forms.ModelForm):
    class Meta:
        model = DepotHolding
        fields = [
            "asset_account",
            "name",
            "isin",
            "ticker",
            "asset_class",
            "quantity",
            "latest_price",
            "currency",
            "as_of_date",
            "payout_date",
            "payout_amount",
            "annual_distribution_rate",
            "distribution_cadence",
            "notes",
        ]
        widgets = {"as_of_date": DateInput(), "payout_date": DateInput()}

    def __init__(self, *args, household=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.household = household
        if household:
            self.fields["asset_account"].queryset = household.accounts.filter(account_type=AssetAccount.AccountType.DEPOT)
        self.fields["payout_date"].help_text = "Optional maturity or payout date for bonds and target-maturity ETFs."
        self.fields["payout_amount"].help_text = (
            "Optional expected cash amount at payout. Use this when the bond should mature above or below today's value."
        )
        self.fields["annual_distribution_rate"].help_text = (
            "Recurring quarterly/annual distribution yield for this holding specifically, e.g. a distributing ETF "
            "or dividend stock. Keep 0 for accumulating funds. Only applies when the depot is valued by summed holdings."
        )

    def clean_currency(self):
        return validate_household_currency(self.cleaned_data.get("currency"), self.household)


class DebtForm(forms.ModelForm):
    account = LoanAccountChoiceField(queryset=AssetAccount.objects.none(), widget=AccountBalanceSelect)
    source_account = AccountChoiceField(queryset=AssetAccount.objects.none(), required=False)

    class Meta:
        model = Debt
        fields = [
            "account",
            "source_account",
            "name",
            "current_principal",
            "annual_interest_rate",
            "monthly_payment",
            "start_month",
            "end_month",
            "fixed_interest_until",
            "refinance_from_month",
            "refinance_annual_interest_rate",
            "refinance_monthly_payment",
            "interest_only_until",
            "annual_extra_payment",
            "extra_payment_month",
            "is_active",
            "notes",
        ]
        widgets = {
            "start_month": DateInput(),
            "end_month": DateInput(),
            "fixed_interest_until": DateInput(),
            "refinance_from_month": DateInput(),
            "interest_only_until": DateInput(),
        }

    def __init__(self, *args, household=None, **kwargs):
        super().__init__(*args, **kwargs)
        if household:
            used_account_ids = list(Debt.objects.filter(household=household).values_list("account_id", flat=True))
            if self.instance.pk and self.instance.account_id in used_account_ids:
                used_account_ids.remove(self.instance.account_id)
            account_queryset = household.accounts.filter(account_type=AssetAccount.AccountType.LOAN)
            self.fields["account"].queryset = account_queryset.exclude(id__in=used_account_ids)
            self.fields["source_account"].queryset = cash_flow_accounts(household)
            if not self.instance.pk and self.fields["account"].queryset.count() == 1:
                account = self.fields["account"].queryset.first()
                self.fields["account"].initial = account
                self.fields["current_principal"].initial = abs(account.balance)
        else:
            self.fields["source_account"].queryset = AssetAccount.objects.none()
        self.fields["current_principal"].required = False
        self.fields["account"].help_text = "The loan/liability account this repayment plan belongs to. Create the loan account first if it is missing."
        self.fields["source_account"].label = "Payment account"
        self.fields["source_account"].help_text = "Cash or savings account used for monthly debt payments and yearly extra repayments. Blank uses the household default operating account."
        self.fields["name"].help_text = "A readable name for this repayment plan, for example Main mortgage."
        self.fields["current_principal"].label = "Current principal"
        self.fields["current_principal"].help_text = "The amount still owed today. Saving the debt keeps the linked loan account balance in sync."
        self.fields["annual_interest_rate"].label = "Current annual interest rate"
        self.fields["annual_interest_rate"].help_text = "Nominal annual rate during the current phase, for example 3.60 for 3.60%."
        self.fields["monthly_payment"].label = "Current monthly payment"
        self.fields["monthly_payment"].help_text = "Total monthly payment during the current phase. The projection splits this into interest and principal."
        self.fields["start_month"].help_text = "First month this debt plan applies. Leave blank if it already exists at the projection start."
        self.fields["end_month"].help_text = "Optional planned final month. If principal remains at this date, data quality will warn you."
        self.fields["fixed_interest_until"].help_text = "Last month of the current fixed-interest period (Zinsbindung). Refinance assumptions start after this unless overridden."
        self.fields["refinance_from_month"].help_text = "Optional custom month for the next phase. Leave blank to start the month after fixed interest ends."
        self.fields["refinance_annual_interest_rate"].label = "Next-phase annual interest rate"
        self.fields["refinance_annual_interest_rate"].help_text = "Assumed rate after the fixed-interest period. For a full payoff at that point, enter 0.00 here and a payoff-sized payment below."
        self.fields["refinance_monthly_payment"].label = "Next-phase monthly payment"
        self.fields["refinance_monthly_payment"].help_text = "Assumed payment after refinancing. To model full repayment after fixed interest, enter an amount larger than the expected remaining principal; LiF caps the final payment at the balance owed."
        self.fields["interest_only_until"].help_text = "Optional interest-only phase. Until this month, payments cover interest only and principal stays flat."
        self.fields["annual_extra_payment"].label = "Yearly extra repayment"
        self.fields["annual_extra_payment"].help_text = "Optional yearly Sondertilgung on top of the monthly payment."
        self.fields["extra_payment_month"].help_text = "Calendar month 1-12 for the yearly extra repayment. Leave blank to use the debt start month."

    def clean(self):
        cleaned_data = super().clean()
        principal = cleaned_data.get("current_principal")
        account = cleaned_data.get("account")
        if principal is None and account:
            cleaned_data["current_principal"] = abs(account.balance)
        return cleaned_data


class IncomeInvestmentForm(forms.ModelForm):
    start_month = forms.DateField(input_formats=["%Y-%m"], widget=MonthInput())
    duration_years = forms.IntegerField(
        min_value=1,
        max_value=80,
        required=False,
        label="Duration in years",
        help_text="Optional. If set, LiF calculates the end month from the start month.",
    )
    end_month = forms.DateField(
        input_formats=["%Y-%m"],
        widget=MonthInput(),
        required=False,
        help_text="Optional when duration is set. Income is included through this month.",
    )

    class Meta:
        model = IncomeInvestment
        fields = [
            "name",
            "investment_type",
            "principal",
            "source_account",
            "monthly_income",
            "annual_growth_rate",
            "currency",
            "start_month",
            "duration_years",
            "end_month",
            "is_active",
            "notes",
        ]

    def __init__(self, *args, household=None, **kwargs):
        super().__init__(*args, **kwargs)
        if household:
            self.fields["source_account"].queryset = cash_flow_accounts(household)
        else:
            self.fields["source_account"].queryset = AssetAccount.objects.none()
        self.fields["source_account"].label = "Funding account"
        self.fields["source_account"].help_text = (
            "Cash or savings account the principal is paid from when the investment starts. Only applied to "
            "purchases at or after the plan start. Leave blank for investments you already own and paid for."
        )
        self.fields["principal"].help_text = (
            "Capital invested or tied up in the project. Used for yield context; deducted from the funding "
            "account at the start month only when a funding account is set (and the start is in the future)."
        )
        self.fields["monthly_income"].help_text = (
            "Expected cash income per month. For irregular solar payments, use a monthly average."
        )
        self.fields["annual_growth_rate"].label = "Annual income growth"
        self.fields["annual_growth_rate"].help_text = (
            "Leave blank to use the household default income growth. Enter 0.00 to keep this investment income flat."
        )

    def clean(self):
        cleaned_data = super().clean()
        start_month = cleaned_data.get("start_month")
        duration_years = cleaned_data.get("duration_years")
        end_month = cleaned_data.get("end_month")
        if start_month and duration_years:
            cleaned_data["end_month"] = add_months(start_month, duration_years * 12 - 1)
        elif not end_month:
            self.add_error("end_month", "Set an end month or provide a duration in years.")
        return cleaned_data


class PrivateLoanReceivableForm(forms.ModelForm):
    disbursement_month = forms.DateField(input_formats=["%Y-%m"], widget=MonthInput(), required=False)
    start_month = forms.DateField(input_formats=["%Y-%m"], widget=MonthInput(), required=False)
    end_month = forms.DateField(input_formats=["%Y-%m"], widget=MonthInput(), required=True)
    source_account = AccountChoiceField(queryset=AssetAccount.objects.none(), required=True)

    class Meta:
        model = PrivateLoanReceivable
        fields = [
            "name",
            "borrower",
            "source_account",
            "current_principal",
            "annual_interest_rate",
            "interest_tax_rate",
            "monthly_interest_income",
            "monthly_principal_repayment",
            "currency",
            "disbursement_month",
            "start_month",
            "end_month",
            "is_gift",
            "is_active",
            "notes",
        ]

    def __init__(self, *args, household=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.household = household
        if household and not self.instance.pk:
            self.fields["currency"].initial = household.currency
            self.fields["interest_tax_rate"].initial = household.capital_gains_tax_rate
        if household:
            self.fields["source_account"].queryset = cash_flow_accounts(household)
        self.fields["source_account"].label = "Repayment account"
        self.fields["source_account"].help_text = (
            "Cash or savings account that originally funded the loan. Interest, monthly Tilgung, and the final repayment return here."
        )
        self.fields["current_principal"].label = "Current principal owed"
        self.fields["current_principal"].help_text = "Amount the borrower still owes you today."
        self.fields["annual_interest_rate"].label = "Annual Zins"
        self.fields["annual_interest_rate"].help_text = "Nominal yearly interest rate. LiF calculates monthly interest from the remaining principal."
        self.fields["interest_tax_rate"].label = "Interest tax rate"
        self.fields["interest_tax_rate"].help_text = "Simple flat tax on interest income. Defaults to the household capital gains tax rate."
        self.fields["monthly_interest_income"].label = "Manual monthly net Zins"
        self.fields["monthly_interest_income"].help_text = "Fallback for unusual agreements. Used only when annual Zins is 0%."
        self.fields["monthly_principal_repayment"].label = "Monthly Tilgung"
        self.fields["monthly_principal_repayment"].help_text = (
            "Freely entered monthly principal repayment. Use 0.00 when the borrower only pays interest and returns all principal at the end."
        )
        self.fields["disbursement_month"].label = "Disbursement month"
        self.fields["disbursement_month"].help_text = (
            "Month the money is paid out from the repayment account. Leave blank if already lent "
            "(principal counts as a receivable now). Set a future month to keep the cash until then."
        )
        self.fields["start_month"].help_text = "First repayment month. Leave blank if repayments apply immediately."
        self.fields["end_month"].help_text = "Required final repayment month. Any principal still outstanding is paid back here."

    def clean_currency(self):
        return validate_household_currency(self.cleaned_data.get("currency"), self.household)


class RealEstateForm(forms.ModelForm):
    acquisition_month = forms.DateField(input_formats=["%Y-%m"], widget=MonthInput(), required=False)
    sale_month = forms.DateField(input_formats=["%Y-%m"], widget=MonthInput(), required=False)
    source_account = AccountChoiceField(queryset=AssetAccount.objects.none(), required=False)
    sale_proceeds_account = AccountChoiceField(queryset=AssetAccount.objects.none(), required=False)
    debts = forms.ModelMultipleChoiceField(
        queryset=Debt.objects.none(),
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Mortgages / loans",
        help_text="Loans financing this property — a property can carry several. All are paid off when it is sold.",
    )

    class Meta:
        model = RealEstate
        fields = [
            "name",
            "use",
            "current_value",
            "annual_appreciation_rate",
            "currency",
            "acquisition_month",
            "down_payment",
            "acquisition_costs",
            "source_account",
            "monthly_costs",
            "saved_monthly_rent",
            "monthly_rent",
            "vacancy_rate",
            "rent_tax_rate",
            "sale_month",
            "sale_costs_rate",
            "capital_gains_tax_rate",
            "sale_proceeds_account",
            "is_active",
            "notes",
        ]

    def __init__(self, *args, household=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.household = household
        if household and not self.instance.pk:
            self.fields["currency"].initial = household.currency
        if household:
            accounts = cash_flow_accounts(household)
            self.fields["source_account"].queryset = accounts
            self.fields["sale_proceeds_account"].queryset = accounts
            self.fields["debts"].queryset = household.debts.select_related("account")
        else:
            self.fields["source_account"].queryset = AssetAccount.objects.none()
            self.fields["sale_proceeds_account"].queryset = AssetAccount.objects.none()
            self.fields["debts"].queryset = Debt.objects.none()
        if self.instance.pk:
            self.fields["debts"].initial = self.instance.debts.all()
        self.fields["source_account"].label = "Funding and costs account"
        self.fields["source_account"].help_text = "Cash or savings account used for down payment, acquisition costs, and monthly carrying costs."
        self.fields["current_value"].help_text = "Market value today, or expected market value at acquisition for a future purchase."
        self.fields["annual_appreciation_rate"].help_text = "Nominal annual property appreciation. Can be negative."
        self.fields["acquisition_month"].help_text = "Leave blank if already owned. Set a future month to model a purchase."
        self.fields["down_payment"].help_text = "Cash paid into the property at acquisition. This is an asset swap."
        self.fields["acquisition_costs"].help_text = "Notary, tax, agent, and similar buying costs. These reduce net worth."
        self.fields["debts"].help_text = (
            "Loans financing this property — a property can carry several (create them under Debts first). "
            "Future purchases activate these debts when the property is acquired, and all are paid off on sale."
        )
        self.fields["monthly_costs"].help_text = "Maintenance, insurance, property tax, Hausgeld, and similar costs."
        self.fields["saved_monthly_rent"].help_text = (
            "Residence only. Use this when you keep a comparable rent expense in rules and want ownership to offset it."
        )
        self.fields["monthly_rent"].help_text = "Investment property only. Gross monthly rent before vacancy and tax."
        self.fields["vacancy_rate"].help_text = "Investment property only. Percent of rent lost to vacancy."
        self.fields["rent_tax_rate"].help_text = "Investment property only. Simple flat tax on rent after vacancy."
        self.fields["sale_month"].help_text = "Optional month to sell the property and convert equity to cash."
        self.fields["sale_costs_rate"].help_text = "Selling costs as a percent of sale price."
        self.fields["capital_gains_tax_rate"].help_text = "Optional tax on appreciation since current/acquisition value. Keep 0 when not applicable."
        self.fields["sale_proceeds_account"].help_text = "Cash or savings account where net sale proceeds land. Blank uses the household default operating account."

    def clean_currency(self):
        return validate_household_currency(self.cleaned_data.get("currency"), self.household)

    def _link_debts(self):
        # `debts` is a reverse FK (Debt.real_estate), so point the selected debts at
        # this property and release any that were unselected.
        instance = self.instance
        selected = set(self.cleaned_data.get("debts", []))
        for debt in list(instance.debts.all()):
            if debt not in selected:
                debt.real_estate = None
                debt.save(update_fields=["real_estate"])
        for debt in selected:
            if debt.real_estate_id != instance.pk:
                debt.real_estate = instance
                debt.save(update_fields=["real_estate"])

    def save(self, commit=True):
        instance = super().save(commit=commit)
        if commit:
            self._link_debts()
        else:
            original_save_m2m = self.save_m2m

            def save_m2m():
                original_save_m2m()
                self._link_debts()

            self.save_m2m = save_m2m
        return instance


class RealEstateTransferPlanForm(forms.ModelForm):
    transfer_month = forms.DateField(input_formats=["%Y-%m"], widget=MonthInput())

    class Meta:
        model = RealEstateTransferPlan
        fields = [
            "name",
            "property_item",
            "giver",
            "recipient",
            "transfer_month",
            "ownership_percent",
            "taxable_gift_value",
            "allowance_amount",
            "allowance_window_years",
            "retained_niessbrauch",
            "niessbrauch_annual_value",
            "notes",
            "is_active",
        ]

    def __init__(self, *args, household=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.household = household
        if household:
            self.fields["property_item"].queryset = household.properties.all()
            self.fields["giver"].queryset = household.people.all()
            self.fields["recipient"].queryset = household.people.all()
        else:
            self.fields["property_item"].queryset = RealEstate.objects.none()
            self.fields["giver"].queryset = Person.objects.none()
            self.fields["recipient"].queryset = Person.objects.none()
        self.fields["taxable_gift_value"].help_text = (
            "Enter the planning value after any Nießbrauch valuation reduction. LiF does not calculate legal tax value."
        )
        self.fields["retained_niessbrauch"].label = "Retain Nießbrauch / right to live there"
        self.fields["niessbrauch_annual_value"].help_text = (
            "Optional documentation value. The forecast uses saved rent on the property for ongoing living benefit."
        )

    def clean(self):
        cleaned_data = super().clean()
        giver = cleaned_data.get("giver")
        recipient = cleaned_data.get("recipient")
        ownership_percent = cleaned_data.get("ownership_percent")
        if giver and recipient and giver == recipient:
            self.add_error("recipient", "Giver and recipient must be different people.")
        if ownership_percent is not None and not (Decimal("0.01") <= ownership_percent <= Decimal("100.00")):
            self.add_error("ownership_percent", "Ownership percent must be between 0.01 and 100.00.")
        return cleaned_data


class RetirementPlanForm(forms.ModelForm):
    class Meta:
        model = RetirementPlan
        fields = [
            "person",
            "name",
            "vehicle_type",
            "current_pension_points",
            "expected_annual_points",
            "pension_value_per_point",
            "private_monthly_pension",
            "retirement_start_month",
            "end_month",
            "annual_adjustment_rate",
            "monthly_contribution",
            "contribution_start_month",
            "contribution_end_month",
            "contribution_relief_rate",
            "payout_taxable_rate",
            "payout_health_insurance_rate",
            "is_active",
            "notes",
        ]
        widgets = {
            "current_pension_points": forms.NumberInput(attrs={"step": "0.0001"}),
            "retirement_start_month": DateInput(),
            "end_month": DateInput(),
            "contribution_start_month": MonthInput(),
            "contribution_end_month": MonthInput(),
        }

    def __init__(self, *args, household=None, **kwargs):
        super().__init__(*args, **kwargs)
        if household:
            self.fields["person"].queryset = household.people.filter(role=Person.Role.ADULT)
        self.fields["vehicle_type"].required = False
        self.fields["vehicle_type"].initial = RetirementPlan.VehicleType.STATUTORY
        self.fields["contribution_start_month"].input_formats = ["%Y-%m"]
        self.fields["contribution_end_month"].input_formats = ["%Y-%m"]
        for field_name, initial in {
            "monthly_contribution": Decimal("0.00"),
            "contribution_relief_rate": Decimal("0.00"),
            "payout_taxable_rate": Decimal("100.00"),
            "payout_health_insurance_rate": Decimal("100.00"),
        }.items():
            self.fields[field_name].required = False
            self.fields[field_name].initial = initial

    def clean(self):
        cleaned_data = super().clean()
        defaults = {
            "vehicle_type": RetirementPlan.VehicleType.STATUTORY,
            "monthly_contribution": Decimal("0.00"),
            "contribution_relief_rate": Decimal("0.00"),
            "payout_taxable_rate": Decimal("100.00"),
            "payout_health_insurance_rate": Decimal("100.00"),
        }
        for field_name, default in defaults.items():
            if cleaned_data.get(field_name) is None:
                cleaned_data[field_name] = default
        return cleaned_data


class EquityGrantForm(forms.ModelForm):
    class Meta:
        model = EquityGrant
        fields = [
            "person",
            "name",
            "grant_type",
            "account",
            "gross_vest_value",
            "withholding_rate",
            "cadence",
            "first_vest_month",
            "last_vest_month",
            "currency",
            "is_active",
            "notes",
        ]
        widgets = {
            "first_vest_month": DateInput(),
            "last_vest_month": DateInput(),
        }

    def __init__(self, *args, household=None, **kwargs):
        super().__init__(*args, **kwargs)
        if household:
            self.fields["person"].queryset = household.people.filter(role=Person.Role.ADULT)
            self.fields["account"].queryset = cash_flow_accounts(household)
        else:
            self.fields["account"].queryset = AssetAccount.objects.none()
        self.fields["account"].required = False
        self.fields["account"].label = "Cash-flow account"
        self.fields["account"].help_text = (
            "Optional. Net vesting proceeds land here. Leave blank to use the household default operating account."
        )
        self.fields["gross_vest_value"].help_text = "Gross value per vesting event, not the full grant."
        self.fields["withholding_rate"].help_text = "Estimated withholding for taxes and social contributions."


class ScenarioForm(forms.ModelForm):
    class Meta:
        model = Scenario
        fields = [
            "name",
            "liquid_balance_delta",
            "monthly_income_delta",
            "monthly_expense_delta",
            "is_active",
            "notes",
        ]


class TrueExpenseForm(forms.ModelForm):
    class Meta:
        model = TrueExpense
        fields = [
            "name",
            "category",
            "account",
            "amount",
            "cadence",
            "first_due_month",
            "end_month",
            "is_active",
            "notes",
        ]
        widgets = {
            "first_due_month": DateInput(),
            "end_month": DateInput(),
        }

    def __init__(self, *args, household=None, **kwargs):
        super().__init__(*args, **kwargs)
        if household:
            self.fields["account"].queryset = cash_flow_accounts(household)
        else:
            self.fields["account"].queryset = AssetAccount.objects.none()
        self.fields["account"].required = False
        self.fields["account"].label = "Payment account"
        self.fields["account"].help_text = (
            "Optional. Expense is paid from this account. Leave blank to use the household default operating account."
        )


class ChildMilestoneForm(forms.ModelForm):
    class Meta:
        model = ChildMilestone
        fields = [
            "person",
            "name",
            "start_month",
            "end_month",
            "monthly_cost_delta",
            "monthly_income_delta",
            "is_active",
            "notes",
        ]
        widgets = {
            "start_month": DateInput(),
            "end_month": DateInput(),
        }

    def __init__(self, *args, household=None, **kwargs):
        super().__init__(*args, **kwargs)
        if household:
            self.fields["person"].queryset = household.people.filter(role=Person.Role.CHILD)


class SalaryChangeForm(forms.ModelForm):
    class Meta:
        model = SalaryChange
        fields = [
            "person",
            "name",
            "account",
            "start_month",
            "end_month",
            "monthly_net_income_delta",
            "is_active",
            "notes",
        ]
        widgets = {
            "start_month": DateInput(),
            "end_month": DateInput(),
        }

    def __init__(self, *args, household=None, **kwargs):
        super().__init__(*args, **kwargs)
        if household:
            self.fields["person"].queryset = household.people.filter(role=Person.Role.ADULT)
            self.fields["account"].queryset = cash_flow_accounts(household)
        else:
            self.fields["account"].queryset = AssetAccount.objects.none()
        self.fields["account"].required = False
        self.fields["account"].label = "Cash-flow account"
        self.fields["account"].help_text = (
            "Optional. Salary delta lands here. Leave blank to use the household default operating account."
        )
        self.fields["monthly_net_income_delta"].help_text = (
            "Enter the monthly net difference from the current salary rule, not the new total salary. "
            "Example: 3,200 -> 3,700 means +500. Use a negative value for a reduction."
        )
