import calendar
import datetime
import decimal

from django.test import TestCase

from claim.gql_mutations import processing_claim
from claim.models import ClaimDedRem, Claim
from claim.test_helpers import (
    create_test_claim,
    create_test_claimservice,
    create_test_claimitem,
)
from claim_batch.services import process_batch
from contribution.test_helpers import create_test_payer, create_test_premium
from contribution_plan.tests.helpers import create_test_payment_plan
from core.test_helpers import create_test_interactive_user, create_admin_role
from insuree.test_helpers import create_test_insuree
from medical.test_helpers import create_test_service, create_test_item
from medical_pricelist.test_helpers import (
    add_service_to_hf_pricelist,
    add_item_to_hf_pricelist,
    create_test_item_pricelist,
    create_test_service_pricelist
)
from policy.test_helpers import create_test_policy
from product.models import ProductItemOrService
from product.test_helpers import (
    create_test_product,
    create_test_product_service,
    create_test_product_item,
)
from location.test_helpers import (
    create_test_health_facility,
    create_test_health_catchment,
    create_test_village
)

from datetime import date, timedelta
_TEST_USER_NAME = "test_batch_run"
_TEST_USER_PWD = "test_batch_run"
_TEST_DATA_USER = {
    "username": _TEST_USER_NAME,
    "last_name": _TEST_USER_NAME,
    "password": _TEST_USER_PWD,
    "other_names": _TEST_USER_NAME,
    "user_types": "INTERACTIVE",
    "language": "en",
    "roles": [create_admin_role().id],
}


class BatchRunWithCapitationPaymentTest(TestCase):

    def setUp(self) -> None:
        super(BatchRunWithCapitationPaymentTest, self).setUp()
        self.user = create_test_interactive_user(username=_TEST_DATA_USER["username"])

    def test_simple_batch(self):
        """
        This test creates a claim, submits it so that it gets dedrem entries,
        then submits a review rejecting part of it, then process the claim.
        It should not be processed (which was ok) but the dedrem should be deleted.
        """
        test_village = create_test_village()
        test_village.parent
        test_region = test_village.parent.parent.parent
        test_district = test_village.parent.parent
        # Given
        insuree = create_test_insuree(custom_props={'current_village': test_village})
        self.assertIsNotNone(insuree)
        service = create_test_service("A", custom_props={"name": "test_simple_batch"})
        item = create_test_item("A", custom_props={"name": "test_simple_batch"})

        product = create_test_product(
            "TESTCAPI",
            custom_props={
                "name": "simplebatch",
                "lump_sum": 10_000,
                "location_id": test_region.id
            },
        )
        create_test_payment_plan(
            product=product,
            calculation="0a1b6d54-5681-4fa6-ac47-2a99c235eaa8",
            custom_props={
                'periodicity': 1,
                'date_valid_from': '2019-01-01',
                'date_valid_to': '2050-01-01',
                'json_ext': {
                    'calculation_rule': {
                        'hf_level_1': 'H',
                        'hf_sublevel_1': "null",
                        'hf_level_2': 'D',
                        'hf_sublevel_2': "null",
                        'hf_level_3': 'C',
                        'hf_sublevel_3': "null",
                        'hf_level_4': "null",
                        'hf_sublevel_4': "null",
                        'distr_1': 100,
                        'distr_2': 100,
                        'distr_3': 100,
                        'distr_4': 100,
                        'distr_5': 100,
                        'distr_6': 100,
                        'distr_7': 100,
                        'distr_8': 100,
                        'distr_9': 100,
                        'distr_10': 100,
                        'distr_11': 100,
                        'distr_12': 100,
                        'claim_type': 'B',
                        'weight_adjusted_amount': 100,
                        "share_contribution": 100,
                        "weight_population": 0,
                        "weight_number_families": 0,
                        "weight_insured_population": 0,
                        "weight_number_insured_families": 0,
                        "weight_number_visits": 0
                    }
                }
            }
        )

        create_test_product_service(
            product,
            service,
            custom_props={"price_origin": ProductItemOrService.ORIGIN_RELATIVE},
        )
        create_test_product_item(
            product,
            item,
            custom_props={"price_origin": ProductItemOrService.ORIGIN_RELATIVE},
        )
        policy = create_test_policy(
            product,
            insuree,
            link=True,
            custom_props={
                'effective_date': date.today() - timedelta(days=200),
                'expiry_date': date.today() + timedelta(days=165),
                'start_date': date.today() - timedelta(days=200),
                'value': 1000
            }
        )
        payer = create_test_payer()
        create_test_premium(
            policy_id=policy.id, custom_props={
                "payer_id": payer.id,
                'amount': 1000,
                'pay_date': date.today() - timedelta(days=200),
                'created_date': datetime.datetime.now() - timedelta(days=200)
            })
        test_item_price_list = create_test_item_pricelist(test_region.id)
        test_service_price_list = create_test_service_pricelist(test_region.id)
        # create hf and attach item/services pricelist
        test_health_facility = create_test_health_facility(
            'HFT',
            test_district.id,
            custom_props={"services_pricelist_id": test_service_price_list.id,
                          "items_pricelist_id": test_item_price_list.id}
        )
        create_test_health_catchment(test_health_facility, test_village)
        add_service_to_hf_pricelist(service, test_health_facility)
        add_item_to_hf_pricelist(item, test_health_facility)

        claim1 = create_test_claim(
            {"claimed": 500.0, "insuree_id": insuree.id, 'health_facility_id': test_health_facility.id})
        service1 = create_test_claimservice(
            claim1, custom_props={"price_asked": 100, "service_id": service.id, "qty_provided": 2}
        )
        item1 = create_test_claimitem(
            claim1, "A", custom_props={"price_asked": 100, "item_id": item.id, "qty_provided": 3}
        )
        errors = processing_claim(claim1, self.user, True)
        _, days_in_month = calendar.monthrange(claim1.validity_from.year, claim1.validity_from.month)
        # add process stamp for claim to not use the process_stamp with now()
        self.assertEqual(len(errors), 0 , "Claim processing failed")
        # Make sure that the dedrem was generated
        dedrem = ClaimDedRem.objects.filter(claim=claim1).first()
        self.assertIsNotNone(dedrem, "No demRem for Claim")
        self.assertEqual(dedrem.rem_g, 500), "Wrong DemRem amount"  # 100*2 + 100*3
        # renumerated should be Null
        self.assertEqual(claim1.remunerated, None, "Claim remunerated when it should not")

        # When
        process_batch(
            self.user.id_for_audit,
            product,
            claim1.date_processed.month,
            claim1.date_processed.year
        )

        claim1.refresh_from_db()
        item1.refresh_from_db()
        service1.refresh_from_db()

        self.assertEqual(claim1.status, Claim.STATUS_VALUATED, "Claim status should be valuated but it is not")
        self.assertNotEqual(item1.price_valuated, item1.price_adjusted)
        self.assertNotEqual(service1.price_valuated, service1.price_adjusted)
        # based on calculation - should be 402.31 per item and service
        # Contribution 1000 ->
        # share contribution 100%
        # total remunerated 500
        # index = (1000 / 365  * day_in mount) / 500
        # value = index * etlem value
        expected_value = round(decimal.Decimal((1000 / 365 * days_in_month / 500 * 100)), 2)
        self.assertEqual(item1.price_valuated, expected_value)
        self.assertEqual(service1.price_valuated, expected_value)
        self.assertEqual(claim1.valuated, service1.price_valuated + item1.price_valuated)
