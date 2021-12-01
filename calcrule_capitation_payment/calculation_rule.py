from calcrule_capitation_payment.apps import AbsCalculationRule
from calcrule_capitation_payment.config import CLASS_RULE_PARAM_VALIDATION, \
    DESCRIPTION_CONTRIBUTION_VALUATION, FROM_TO
from calcrule_capitation_payment.utils import capitation_report_data_for_submit, \
    get_capitation_region_and_district_codes, check_bill_exist
from invoice.services import BillService
from calcrule_capitation_payment.converters import BatchRunToBillConverter, CapitationPaymentToBillItemConverter
from core.signals import *
from core import datetime
from django.contrib.contenttypes.models import ContentType
from contribution_plan.models import PaymentPlan
from product.models import Product
from core.models import User
from claim_batch.models import BatchRun, CapitationPayment
from location.models import HealthFacility


class CapitationPaymentCalculationRule(AbsCalculationRule):
    version = 1
    uuid = "0a1b6d54-5681-4fa6-ac47-2a99c235eaa8"
    calculation_rule_name = "payment: capitation"
    description = DESCRIPTION_CONTRIBUTION_VALUATION
    impacted_class_parameter = CLASS_RULE_PARAM_VALIDATION
    date_valid_from = datetime.datetime(2000, 1, 1)
    date_valid_to = None
    status = "active"
    from_to = FROM_TO
    type = "account_payable"
    sub_type = "third_party_payment"

    signal_get_rule_name = Signal(providing_args=[])
    signal_get_rule_details = Signal(providing_args=[])
    signal_get_param = Signal(providing_args=[])
    signal_get_linked_class = Signal(providing_args=[])
    signal_calculate_event = Signal(providing_args=[])
    signal_convert_from_to = Signal(providing_args=[])

    @classmethod
    def ready(cls):
        now = datetime.datetime.now()
        condition_is_valid = (now >= cls.date_valid_from and now <= cls.date_valid_to) \
            if cls.date_valid_to else (now >= cls.date_valid_from and cls.date_valid_to is None)
        if condition_is_valid:
            if cls.status == "active":
                # register signals getParameter to getParameter signal and getLinkedClass ot getLinkedClass signal
                cls.signal_get_rule_name.connect(cls.get_rule_name, dispatch_uid="on_get_rule_name_signal")
                cls.signal_get_rule_details.connect(cls.get_rule_details, dispatch_uid="on_get_rule_details_signal")
                cls.signal_get_param.connect(cls.get_parameters, dispatch_uid="on_get_param_signal")
                cls.signal_get_linked_class.connect(cls.get_linked_class, dispatch_uid="on_get_linked_class_signal")
                cls.signal_calculate_event.connect(cls.run_calculation_rules, dispatch_uid="on_calculate_event_signal")
                cls.signal_convert_from_to.connect(cls.run_convert, dispatch_uid="on_convert_from_to")

    @classmethod
    def active_for_object(cls, instance, context, type, sub_type):
        return instance.__class__.__name__ == "PaymentPlan" \
               and context in ["BatchValuation", "BatchPayment", "IndividualPayment", "IndividualValuation"] \
               and cls.check_calculation(instance)

    @classmethod
    def check_calculation(cls, instance):
        class_name = instance.__class__.__name__
        match = False
        if class_name == "PaymentPlan":
            match = cls.uuid == str(instance.calculation)
        elif class_name == "BatchRun":
            # BatchRun → Product or Location if no prodcut
            match = cls.check_calculation(instance.location)
        elif class_name == "HealthFacility":
            #  HF → location
            match = cls.check_calculation(instance.location)
        elif class_name == "Location":
            #  location → ProductS (Product also related to Region if the location is a district)
            if instance.type in ["D", "R"]:
                products = Product.objects.filter(location=instance, validity_to__isnull=True)
                for product in products:
                    if cls.check_calculation(product):
                        match = True
                        break
        elif class_name == "Product":
            # if product → paymentPlans
            payment_plans = PaymentPlan.objects.filter(benefit_plan=instance, is_deleted=False)
            for pp in payment_plans:
                if cls.check_calculation(pp):
                    match = True
                    break
        return match

    @classmethod
    def calculate(cls, instance, **kwargs):
        context = kwargs.get('context', None)
        class_name = instance.__class__.__name__
        if instance.__class__.__name__ == "PaymentPlan":
            if context == "BatchPayment":
                # get all valuated claims that should be evaluated
                #  with capitation that matches args (existing function develop in TZ scope)
                audit_user_id, location_id, period, year = cls._get_batch_run_parameters(**kwargs)

                # process capitation
                capitation_report_data_for_submit(audit_user_id, location_id, period, year)

                # do the conversion based on those params after generating capitation
                product = instance.benefit_plan
                batch_run, capitation_payment, capitation_hf_list, user = \
                    cls._process_capitation_results(product, **kwargs)

                for chf in capitation_hf_list:
                    capitation_payments = capitation_payment.filter(health_facility__id=chf['health_facility'])
                    hf = HealthFacility.objects.get(id=chf['health_facility'])
                    # take batch run to convert capitation payments into bill per HF
                    cls.run_convert(
                        instance=batch_run,
                        convert_to='Bill',
                        user=user,
                        health_facility=hf,
                        capitation_payments=capitation_payments,
                        payment_plan=instance,
                        context=context
                    )
            elif context == "BatchValuation":
                pass
            elif context == "IndividualPayment":
                pass
            elif context == "IndividualValuation":
                pass

    @classmethod
    def get_linked_class(cls, sender, class_name, **kwargs):
        list_class = []
        if class_name != None:
            model_class = ContentType.objects.filter(model=class_name).first()
            if model_class:
                model_class = model_class.model_class()
                list_class = list_class + \
                             [f.remote_field.model.__name__ for f in model_class._meta.fields
                              if f.get_internal_type() == 'ForeignKey' and f.remote_field.model.__name__ != "User"]
        else:
            list_class.append("Calculation")
        # because we have calculation in PaymentPlan
        #  as uuid - we have to consider this case
        if class_name == "PaymentPlan":
            list_class.append("Calculation")
        return list_class

    @classmethod
    def convert(cls, instance, convert_to, **kwargs):
        context = kwargs.get('context', None)
        results = {}
        if context == "BatchPayment":
            hf = kwargs.get('health_facility', None)
            capitation_payments = kwargs.get('capitation_payments', None)
            payment_plan = kwargs.get('payment_plan', None)
            if check_bill_exist(instance, hf):
                convert_from = instance.__class__.__name__
                if convert_from == "BatchRun":
                    results = cls._convert_capitation_payment(instance, hf, capitation_payments, payment_plan)
                results['user'] = kwargs.get('user', None)
                BillService.bill_create(convert_results=results)
        return results

    @classmethod
    def _get_batch_run_parameters(cls, **kwargs):
        audit_user_id = kwargs.get('audit_user_id', None)
        location_id = kwargs.get('location_id', None)
        period = kwargs.get('period', None)
        year = kwargs.get('year', None)
        return audit_user_id, location_id, period, year

    @classmethod
    def _process_capitation_results(cls, product, **kwargs):
        audit_user_id, location_id, period, year = cls._get_batch_run_parameters(**kwargs)
        # if this is trigerred by batch_run - take user data from audit_user_id
        if audit_user_id:
            user = User.objects.filter(i_user__id=audit_user_id).first()

        # get batch run related to this capitation payment
        batch_run = BatchRun.objects.filter(run_year=year, run_month=period, location_id=location_id, validity_to__isnull=True)
        if batch_run:
           batch_run = batch_run.first()
           region_code, district_code = get_capitation_region_and_district_codes(location_id)
           if district_code:
               capitation_payment = CapitationPayment.objects.filter(
                   product=product,
                   validity_to=None,
                   region_code=region_code,
                   district_code=district_code,
                   year=year,
                   month=period,
                   total_adjusted__gt=0
               )
           else:
               capitation_payment = CapitationPayment.objects.filter(
                   product=product,
                   validity_to=None,
                   region_code=region_code,
                   year=year,
                   month=period,
                   total_adjusted__gt=0
               )

           capitation_hf_list = list(capitation_payment.values('health_facility').distinct())

           return batch_run, capitation_payment, capitation_hf_list, user

    @classmethod
    def _convert_capitation_payment(cls, instance, health_facility, capitation_payments, payment_plan):
        bill = BatchRunToBillConverter.to_bill_obj(
            batch_run=instance,
            health_facility=health_facility,
            payment_plan=payment_plan
        )
        bill_line_items = []
        for cp in capitation_payments.all():
            bill_line_item = CapitationPaymentToBillItemConverter.to_bill_line_item_obj(
                capitation_payment=cp,
                batch_run=instance,
                payment_plan=payment_plan
            )
            bill_line_items.append(bill_line_item)
        return {
            'bill_data': bill,
            'bill_data_line': bill_line_items,
            'type_conversion': 'batch run capitation payment - bill'
        }
