# Generated manually after Django runtime was unavailable in this shell.

from decimal import Decimal

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('planner', '0049_privateloanreceivable_is_gift'),
    ]

    operations = [
        migrations.CreateModel(
            name='RealEstate',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=140)),
                ('use', models.CharField(choices=[('residence', 'Residence'), ('investment', 'Investment property')], default='residence', max_length=20)),
                ('current_value', models.DecimalField(decimal_places=2, max_digits=12)),
                ('annual_appreciation_rate', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=5)),
                ('currency', models.CharField(default='EUR', max_length=3)),
                ('acquisition_month', models.DateField(blank=True, null=True)),
                ('down_payment', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=12)),
                ('acquisition_costs', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=12)),
                ('monthly_costs', models.DecimalField(decimal_places=2, default=Decimal('0.00'), help_text='Maintenance, insurance, property tax, Hausgeld, and similar monthly carrying costs.', max_digits=12)),
                ('monthly_rent', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=12)),
                ('vacancy_rate', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=5)),
                ('rent_tax_rate', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=5)),
                ('sale_month', models.DateField(blank=True, null=True)),
                ('sale_costs_rate', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=5)),
                ('capital_gains_tax_rate', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=5)),
                ('is_active', models.BooleanField(default=True)),
                ('notes', models.TextField(blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('household', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='properties', to='planner.household')),
                ('mortgage', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='financed_properties', to='planner.debt')),
                ('sale_proceeds_account', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='property_sale_proceeds', to='planner.assetaccount')),
                ('source_account', models.ForeignKey(blank=True, help_text='Cash or savings account used for down payment, acquisition costs, and monthly costs.', null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='funded_properties', to='planner.assetaccount')),
            ],
            options={
                'ordering': ['name'],
            },
        ),
        migrations.AlterField(
            model_name='featureflag',
            name='key',
            field=models.CharField(choices=[('analytics', 'Analytics'), ('cash_goals', 'Cash Goals'), ('depot_holdings', 'Depot Holdings'), ('debts', 'Debts'), ('real_estate', 'Real Estate'), ('income_investments', 'Income Investments'), ('retirement_plans', 'Retirement Plans'), ('equity_grants', 'Equity Grants'), ('scenarios', 'Scenarios'), ('true_expenses', 'True Expenses'), ('child_milestones', 'Child Milestones'), ('salary_changes', 'Salary Changes'), ('imports', 'Imports'), ('read_only_mode', 'Read Only Mode'), ('snapshots', 'Snapshots'), ('moneymoney_import', 'Moneymoney Import'), ('ynab_import', 'Ynab Import'), ('multi_language', 'Multi Language'), ('advanced_tax_model', 'Advanced Tax Model'), ('docker_deployment', 'Docker Deployment'), ('mobile_read_only', 'Mobile Read Only'), ('mcp_server', 'Mcp Server')], max_length=80, unique=True),
        ),
    ]
