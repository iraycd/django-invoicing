from __future__ import division

try:
    from collections import OrderedDict
except ImportError:
    from ordereddict import OrderedDict

from decimal import Decimal
from django_countries.fields import CountryField
from django_iban.fields import IBANField, SWIFTBICField
from djmoney.forms.widgets import CURRENCY_CHOICES
from jsonfield import JSONField
from model_utils import Choices
from model_utils.fields import MonitorField

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.urlresolvers import reverse
from django.core.validators import EMPTY_VALUES, MaxValueValidator, MinValueValidator
from django.db import models
from django.db.models import Max
from django.template import Template, Context
from django.utils.timezone import now
from django.utils.translation import ugettext_lazy as _

from invoicing.fields import VATField
from invoicing.managers import InvoiceManager, ItemManager
from invoicing.taxation import TaxationPolicy
from invoicing.taxation.eu import EUTaxationPolicy
from invoicing.utils import import_name


def default_supplier(attribute_lookup):
    supplier = getattr(settings, 'INVOICING_SUPPLIER', None)

    if not supplier:
        return None

    lookup_object = supplier
    for attribute in attribute_lookup.split('.'):
        lookup_object = lookup_object.get(attribute, None)

    return lookup_object


class Invoice(models.Model):
    """
    Model representing Invoice itself.
    It keeps all necessary information described at https://www.gov.uk/vat-record-keeping/vat-invoices
    """
    COUNTER_PERIOD = Choices(
        ('DAILY', _('daily')),
        ('MONTHLY', _('monthly')),
        ('YEARLY', _('yearly'))
    )

    TYPE = Choices(
        ('INVOICE', _(u'Invoice')),
        ('ADVANCE', _(u'Advance invoice')),
        ('PROFORMA', _(u'Proforma invoice')),
        ('VAT_CREDIT_NOTE', _(u'VAT credit note'))
    )

    STATUS = Choices(
        ('NEW', _(u'new')),
        ('SENT', _(u'sent')),
        ('RETURNED', _(u'returned')),
        ('CANCELED', _(u'canceled')),
        ('PAID', _(u'paid'))
    )

    PAYMENT_METHOD = Choices(
        ('BANK_TRANSFER', _(u'bank transfer')),
        ('CASH', _(u'cash')),
        ('CASH_ON_DELIVERY', _(u'cash on delivery')),
        ('PAYMENT_CARD', _(u'payment card'))
    )

    DELIVERY_METHOD = Choices(
        ('PERSONAL_PICKUP', _(u'personal pickup')),
        ('MAILING', _(u'mailing')),
        ('DIGITAL', _(u'digital')),
    )

    CONSTANT_SYMBOL = Choices(
        ('0001', _(u'0001 - Payments for goods based on legal and executable decision from legal authority')),
        ('0008', _(u'0008 - Cashless payments for goods')),
        ('0038', _(u'0038 - Cashless funds for wages')),
        ('0058', _(u'0058 - Cashless penalty and delinquency charges')),
        ('0068', _(u'0068 - Transfer of funds for wages and other personal costs')),
        ('0138', _(u'0138 - Cashless deductions at source')),
        ('0168', _(u'0168 - Cashless payments in loans')),
        ('0178', _(u'0178 - Sales from provided services')),
        ('0298', _(u'0298 - Other cashless transfers')),
        ('0304', _(u'0304 - Prior payments for services')),
        ('0308', _(u'0308 - Cashless payments for services')),
        ('0358', _(u'0358 - Payments dedicated to payout through post offices')),
        ('0379', _(u'0379 - Other income, income from postal order')),
        ('0498', _(u'0498 - Payments in loans')),
        ('0558', _(u'0558 - Cashless other financial payments')),
        ('0934', _(u'0934 - Benefits - prior payments')),
        ('0968', _(u'0968 - Other cashless transfers')),
        ('1144', _(u'1144 - Prior payment - advance')),
        ('1148', _(u'1148 - Payment - current advance')),
        ('1744', _(u'1744 - Accounting of tax at income tax of physical body and corporate body')),
        ('1748', _(u'1748 - Income tax of physical body and corporate body based on declared tax year')),
        ('3118', _(u'3118 - Insurance and empl. contrib. to insur. co. and the Labor Office')),
        ('3344', _(u'3344 - Penalty from message - prior')),
        ('3348', _(u'3348 - Penalty from message')),
        ('3354', _(u'3354 - Insurance payments by insurance companies')),
        ('3558', _(u'3558 - Cashless insurance payments by insurance companies')),
        ('8147', _(u'8147 - Payment (posted together with the instruction)'))
    )

    # General information
    type = models.CharField(_(u'type'), max_length=64, choices=TYPE, default=TYPE.INVOICE)
    number = models.IntegerField(_(u'number'), db_index=True, blank=True)
    full_number = models.CharField(max_length=128, blank=True)
    status = models.CharField(_(u'status'), choices=STATUS, max_length=64, default=STATUS.NEW)
    subtitle = models.CharField(_(u'subtitle'), max_length=255,
        blank=True, null=True, default=None)
    language = models.CharField(_(u'language'), max_length=10, choices=settings.LANGUAGES)
    note = models.CharField(_(u'note'), max_length=255,
        blank=True, null=True, default=_(u'Thank you for using our services.'))
    date_issue = models.DateField(_(u'issue date'))
    date_tax_point = models.DateField(_(u'tax point date'), help_text=_(u'time of supply'))
    date_due = models.DateField(_(u'due date'), help_text=_(u'payment till'))
    date_sent = MonitorField(monitor='status', when=[STATUS.SENT],
        blank=True, null=True, default=None)

    # Payment details
    currency = models.CharField(_(u'currency'), max_length=10, choices=CURRENCY_CHOICES)
    credit = models.DecimalField(_(u'credit'), max_digits=10, decimal_places=2, default=0)
    #already_paid = models.DecimalField(_(u'already paid'), max_digits=10, decimal_places=2, default=0)

    payment_method = models.CharField(_(u'payment method'), choices=PAYMENT_METHOD, max_length=64)
    constant_symbol = models.CharField(_(u'constant symbol'), max_length=64, choices=CONSTANT_SYMBOL,
        blank=True, null=True, default=None)
    variable_symbol = models.PositiveIntegerField(_(u'variable symbol'),
        validators=[MinValueValidator(0), MaxValueValidator(9999999999)],
        blank=True, null=True, default=None)
    specific_symbol = models.PositiveIntegerField(_(u'specific symbol'),
        validators=[MinValueValidator(0), MaxValueValidator(9999999999)],
        blank=True, null=True, default=None)
    reference = models.CharField(_(u'reference'), max_length=140,
        blank=True, null=True, default=None)

    bank_name = models.CharField(_(u'bank name'), max_length=255,
        blank=True, null=True, default=None)
    bank_street = models.CharField(_(u'bank street and number'), max_length=255,
        blank=True, null=True, default=None)
    bank_zip = models.CharField(_(u'bank ZIP'), max_length=255,
        blank=True, null=True, default=None)
    bank_city = models.CharField(_(u'bank city'), max_length=255,
        blank=True, null=True, default=None)
    bank_country = CountryField(_(u'bank country'), max_length=255,
        blank=True, null=True, default=None)
    bank_iban = IBANField(verbose_name=_(u'Account number (IBAN)'), default=None)
    bank_swift_bic = SWIFTBICField(verbose_name=_(u'Bank SWIFT / BIC'), default=None)

    # Issuer details
    supplier_name = models.CharField(_(u'supplier name'), max_length=255, default=None)
    supplier_street = models.CharField(_(u'supplier street and number'), max_length=255,
        blank=True, null=True, default=None)
    supplier_zip = models.CharField(_(u'supplier ZIP'), max_length=255,
        blank=True, null=True, default=None)
    supplier_city = models.CharField(_(u'supplier city'), max_length=255,
        blank=True, null=True, default=None)
    supplier_country = CountryField(_(u'supplier country'), default=None)
    supplier_registration_id = models.CharField(_(u'supplier Reg. No.'), max_length=255,
        blank=True, null=True, default=None)
    supplier_tax_id = models.CharField(_(u'supplier Tax No.'), max_length=255,
        blank=True, null=True, default=None)
    supplier_vat_id = VATField(_(u'supplier VAT No.'),
        blank=True, null=True, default=None)
    supplier_additional_info = JSONField(_(u'supplier additional information'),
        load_kwargs={'object_pairs_hook': OrderedDict},
        blank=True, null=True, default=None)  # for example www or legal matters

    # Contact details
    issuer_name = models.CharField(_(u'issuer name'), max_length=255,
        blank=True, null=True, default=None)
    issuer_email = models.EmailField(_(u'issuer email'),
        blank=True, null=True, default=None)
    issuer_phone = models.CharField(_(u'issuer phone'), max_length=255,
        blank=True, null=True, default=None)

    # Customer details
    customer_name = models.CharField(_(u'customer name'), max_length=255)
    customer_street = models.CharField(_(u'customer street and number'), max_length=255,
        blank=True, null=True, default=None)
    customer_zip = models.CharField(_(u'customer ZIP'), max_length=255,
        blank=True, null=True, default=None)
    customer_city = models.CharField(_(u'customer city'), max_length=255,
        blank=True, null=True, default=None)
    customer_country = CountryField(_(u'customer country'))
    customer_registration_id = models.CharField(_(u'customer Reg. No.'), max_length=255,
        blank=True, null=True, default=None)
    customer_tax_id = models.CharField(_(u'customer Tax No.'), max_length=255,
        blank=True, null=True, default=None)
    customer_vat_id = VATField(_(u'customer VAT No.'),
        blank=True, null=True, default=None)
    customer_additional_info = JSONField(_(u'customer additional information'),
        load_kwargs={'object_pairs_hook': OrderedDict},
        blank=True, null=True, default=None)

    # Shipping details
    shipping_name = models.CharField(_(u'shipping name'), max_length=255,
        blank=True, null=True, default=None)
    shipping_street = models.CharField(_(u'shipping street and number'), max_length=255,
        blank=True, null=True, default=None)
    shipping_zip = models.CharField(_(u'shipping ZIP'), max_length=255,
        blank=True, null=True, default=None)
    shipping_city = models.CharField(_(u'shipping city'), max_length=255,
        blank=True, null=True, default=None)
    shipping_country = CountryField(_(u'shipping country'),
        blank=True, null=True, default=None)

    # Delivery details
    delivery_method = models.CharField(_(u'delivery method'), choices=DELIVERY_METHOD, max_length=64,
        default=DELIVERY_METHOD.PERSONAL_PICKUP)

    # Other
    created = models.DateTimeField(_(u'created'), auto_now_add=True)
    modified = models.DateTimeField(_(u'modified'), auto_now=True)
    objects = InvoiceManager()

    class Meta:
        db_table = 'invoicing_invoices'
        verbose_name = _(u'invoice')
        verbose_name_plural = _(u'invoices')
        ordering = ('date_issue', 'number')

    def __str__(self):
        return self.full_number

    def __unicode__(self):
        return self.full_number

    def save(self, **kwargs):
        if self.number in EMPTY_VALUES:
            self.number = self._get_next_number()

        if self.full_number in EMPTY_VALUES:
            self.full_number = self._get_full_number()

        return super(Invoice, self).save(**kwargs)

    def get_absolute_url(self):
        return reverse('invoicing:invoice_detail', args=(self.pk,))

    def _get_next_number(self):
        """
        Returnes next invoice number based on ``settings.INVOICING_COUNTER_PERIOD``.

        .. warning::

            This is only used to prepopulate ``number`` field on saving new invoice.
            To get invoice number always use ``number`` field.

        .. note::

            To get invoice full number use ``full_number`` field.

        :return: string (generated next number)
        """
        invoice_counter_reset = getattr(settings, 'INVOICING_COUNTER_PERIOD', Invoice.COUNTER_PERIOD.YEARLY)

        important_date = self.date_tax_point  # self.date_issue
        if invoice_counter_reset == Invoice.COUNTER_PERIOD.DAILY:
            relative_invoices = Invoice.objects.filter(date_issue=important_date, type=self.type)

        elif invoice_counter_reset == Invoice.COUNTER_PERIOD.YEARLY:
            relative_invoices = Invoice.objects.filter(date_issue__year=important_date.year, type=self.type)

        elif invoice_counter_reset == Invoice.COUNTER_PERIOD.MONTHLY:
            relative_invoices = Invoice.objects.filter(date_issue__year=important_date.year, date_issue__month=important_date.month, type=self.type)

        else:
            raise ImproperlyConfigured("INVOICING_COUNTER_PERIOD can be set only to these values: DAILY, MONTHLY, YEARLY.")

        last_number = relative_invoices.aggregate(Max('number'))['number__max'] or 0

        return last_number + 1

    def _get_full_number(self):
        """
        Generates on the fly invoice full number from template provided by ``settings.INVOICING_NUMBER_FORMAT``.
        ``Invoice`` object is provided as ``invoice`` variable to the template, therefore all object fields
        can be used to generate full number format.

        .. warning::

            This is only used to prepopulate ``full_number`` field on saving new invoice.
            To get invoice full number always use ``full_number`` field.

        :return: string (generated full number)
        """
        number_format = getattr(settings, "INVOICING_NUMBER_FORMAT", "{{ invoice.date_tax_point|date:'Y' }}/{{ invoice.number }}")
        return Template(number_format).render(Context({'invoice': self}))

    def get_tax_rate(self):
        if self.taxation_policy:
            # There is taxation policy -> get tax rate
            customer_country_code = self.customer_country.code if self.customer_country else None
            supplier_country_code = self.supplier_country.code if self.supplier_country else None
            return self.taxation_policy.get_tax_rate(self.customer_vat_id, customer_country_code, supplier_country_code)
        else:
            # If there is not any special taxation policy, set default tax rate
            return TaxationPolicy.get_default_tax()

    @property
    def taxation_policy(self):
        taxation_policy = getattr(settings, 'INVOICING_TAXATION_POLICY', None)
        if taxation_policy is not None:
            return import_name(taxation_policy)

        # Check if supplier is from EU
        if self.supplier_country:
            if EUTaxationPolicy.is_in_EU(self.supplier_country.code):
                return EUTaxationPolicy

        return None

    @property
    def is_overdue(self):
        return self.date_due < now().date() and self.status not in [self.STATUS.PAID, self.STATUS.CANCELED]

    @property
    def overdue_days(self):
        return (now().date() - self.date_due).days

    @property
    def payment_term(self):
        return (self.date_due - self.date_issue).days if self.total > 0 else 0

    def set_supplier_data(self, supplier):
        self.supplier_name = supplier.get('name')
        self.supplier_street = supplier.get('street', None)
        self.supplier_zip = supplier.get('zip', None)
        self.supplier_city = supplier.get('city', None)
        self.supplier_country = supplier.get('country_code')
        self.supplier_registration_id = supplier.get('registration_id', None)
        self.supplier_tax_id = supplier.get('tax_id', None)
        self.supplier_vat_id = supplier.get('vat_id', None)
        self.supplier_additional_info = supplier.get('additional_info', None)

        bank = supplier.get('bank')
        self.bank_name = bank.get('name')
        self.bank_street = bank.get('street')
        self.bank_zip = bank.get('zip')
        self.bank_city = bank.get('city')
        self.bank_country = bank.get('country_code')
        self.bank_iban = bank.get('iban')
        self.bank_swift_bic = bank.get('swift_bic')

    def set_customer_data(self, customer):
        self.customer_name = customer.get('name')
        self.customer_street = customer.get('street', None)
        self.customer_zip = customer.get('zip', None)
        self.customer_city = customer.get('city', None)
        self.customer_country = customer.get('country_code')
        self.customer_registration_id = customer.get('registration_id', None)
        self.customer_tax_id = customer.get('tax_id', None)
        self.customer_vat_id = customer.get('vat_id', None)
        self.customer_additional_info = customer.get('additional_info', None)
        
    def set_shipping_data(self, shipping):
        self.shipping_name = shipping.get('name', None)
        self.shipping_street = shipping.get('street', None)
        self.shipping_zip = shipping.get('zip', None)
        self.shipping_city = shipping.get('city', None)
        self.shipping_country = shipping.get('country_code', None)

    # http://www.superfaktura.sk/blog/neplatca-dph-vzor-faktury/
    def is_supplier_vat_id_visible(self):
        if self.vat is None and self.supplier_country == self.customer_country:
            return False

        # VAT is not 0
        if self.vat != 0 or self.item_set.filter(tax_rate__gt=0).exists():
            return True

        # VAT is 0, check if customer is from EU and from same country as supplier
        is_EU_customer = EUTaxationPolicy.is_in_EU(self.customer_country.code) if self.customer_country else False

        return is_EU_customer and self.supplier_country != self.customer_country

    @property
    def vat_summary(self):
        #rates_and_sum = self.item_set.all().annotate(base=Sum(F('qty')*F('price_per_unit'))).values('tax_rate', 'base')
        #rates_and_sum = self.item_set.all().values('tax_rate').annotate(Sum('price_per_unit'))
        #rates_and_sum = self.item_set.all().values('tax_rate').annotate(Sum(F('qty')*F('price_per_unit')))

        from django.db import connection
        cursor = connection.cursor()
        cursor.execute('select tax_rate as rate, SUM(quantity*unit_price*(100-discount)/100) as base, ROUND(CAST(SUM(quantity*unit_price*(tax_rate/100)) AS numeric), 2) as vat from invoicing_items where invoice_id = %s group by tax_rate;', [self.pk])

        desc = cursor.description
        return [
            dict(zip([col[0] for col in desc], row))
            for row in cursor.fetchall()
        ]

    @property
    def subtotal(self):
        sum = 0
        for item in self.item_set.all():
            sum += item.subtotal
        return round(sum, 2)

    @property
    def vat(self):
        if len(self.vat_summary) == 1 and self.vat_summary[0]['vat'] is None:
            return None

        vat = 0
        for vat_rate in self.vat_summary:
            vat += vat_rate['vat'] or 0
        return vat

    @property
    def total(self):
        #total = self.subtotal + self.vat  # subtotal with vat
        total = 0
        for vat_rate in self.vat_summary:
            total += float(vat_rate['vat'] or 0) + float(vat_rate['base'])

        #total *= float((100 - float(self.discount)) / 100)  # subtract discount amount
        total -= float(self.credit)  # subtract credit
        #total -= self.already_paid  # subtract already paid
        return round(total, 2)


class Item(models.Model):
    WEIGHT = [(i, i) for i in range(0, 20)]
    UNIT_EMPTY = 'EMPTY'
    UNIT_PIECES = 'PIECES'
    UNIT_HOURS = 'HOURS'
    UNITS = (
        (UNIT_EMPTY, ''),
        (UNIT_PIECES, _(u'pcs.')),
        (UNIT_HOURS, _(u'hours'))
    )

    invoice = models.ForeignKey(Invoice, verbose_name=_(u'invoice'))
    title = models.CharField(_(u'title'), max_length=255)
    quantity = models.DecimalField(_(u'quantity'), max_digits=10, decimal_places=3, default=1)
    unit = models.CharField(_(u'unit'), choices=UNITS, max_length=64, default=UNIT_PIECES)
    unit_price = models.DecimalField(_(u'unit price'), max_digits=10, decimal_places=2)
    discount = models.DecimalField(_(u'discount (%)'), max_digits=4, decimal_places=1, default=0)
    tax_rate = models.DecimalField(_(u'tax rate (%)'), max_digits=3, decimal_places=1, help_text=_(u'VAT rate'),
        blank=True, null=True, default=None)
    tag = models.CharField(_(u'tag'), max_length=128,
        blank=True, null=True, default=None)
    weight = models.IntegerField(_(u'weight'), choices=WEIGHT, help_text=_(u'order'),
        blank=True, null=True, default=0)
    created = models.DateTimeField(_(u'created'), auto_now_add=True)
    modified = models.DateTimeField(_(u'modified'), auto_now=True)
    objects = ItemManager()

    class Meta:
        db_table = 'invoicing_items'
        verbose_name = _(u'item')
        verbose_name_plural = _(u'items')
        ordering = ('-invoice', 'weight', 'created')

    def __unicode__(self):
        return self.title

    @property
    def subtotal(self):
        subtotal = round(self.unit_price * self.quantity, 2)
        return round(Decimal(subtotal) * Decimal((100 - self.discount) / 100), 2)

    @property
    def vat(self):
        return round(self.subtotal * float(self.tax_rate)/100 if self.tax_rate else 0, 2)

    @property
    def unit_price_with_vat(self):
        tax_rate = self.tax_rate if self.tax_rate else 0
        return round(Decimal(self.unit_price) * Decimal((100 + tax_rate) / 100), 2)

    @property
    def total(self):
        return round(self.subtotal + self.vat, 2)

    def save(self, **kwargs):
        # If tax rate is not set while creating new invoice item, set it according billing details
        if self.tax_rate in EMPTY_VALUES and self.pk is None:
            self.tax_rate = self.invoice.get_tax_rate()
        return super(Item, self).save(**kwargs)
