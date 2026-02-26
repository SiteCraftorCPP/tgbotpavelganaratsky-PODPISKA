import aiohttp
import logging
import json
import base64
import asyncio

logger = logging.getLogger(__name__)

class BePaidAPI:
    def __init__(self, shop_id: str, secret_key: str, test_mode: bool = False):
        self.shop_id = shop_id
        self.secret_key = secret_key
        self.base_url = "https://checkout.bepaid.by/ctp/api"
        self.test_mode = test_mode
        self._auth = aiohttp.BasicAuth(login=shop_id, password=secret_key)

    async def create_checkout_link(self, amount: float, currency: str, description: str, 
                                   order_id: str, email: str, notification_url: str = None, 
                                   return_url: str = None):
        """
        Создает ссылку на оплату (Checkout).
        Включает сохранение карты (recurring) если это первая оплата.
        """
        url = f"{self.base_url}/checkouts"
        
        # Минимальная сумма для BYN
        amount_cents = int(amount * 100)

        payload = {
            "checkout": {
                "version": 2.1,
                "test": self.test_mode,
                "transaction_type": "payment",
                "order": {
                    "amount": amount_cents,
                    "currency": currency,
                    "description": description,
                    "tracking_id": order_id,
                },
                "customer": {
                    "email": email
                },
                "settings": {
                    "success_url": return_url,
                    "decline_url": return_url,
                    "fail_url": return_url,
                    "notification_url": notification_url,
                    "language": "ru",
                    # Включаем сохранение карты для последующих списаний
                    "customer_fields": {
                        "visible": ["email"],
                        "read_only": []
                    }
                },
                # Для рекуррентных платежей важно запросить токен
                "payment_method": {
                  "types": ["credit_card"],
                  "credit_card": {
                    "save_card": True 
                  }
                }
            }
        }

        async with aiohttp.ClientSession(auth=self._auth) as session:
            try:
                async with session.post(url, json=payload) as response:
                    data = await response.json()
                    if response.status in (200, 201):
                        return data.get("checkout", {}).get("redirect_url")
                    else:
                        logger.error(f"BePaid create_checkout error: {data}")
                        return None
            except Exception as e:
                logger.error(f"BePaid request failed: {e}")
                return None

    async def charge_recurrent(self, amount: float, currency: str, description: str, 
                               order_id: str, card_token: str, email: str):
        """
        Списывает деньги по сохраненному токену карты.
        Используем endpoint транзакций шлюза (не checkout).
        """
        # Для прямых транзакций URL другой: https://gateway.bepaid.by/transactions/payments
        gateway_url = "https://gateway.bepaid.by/transactions/payments"
        
        amount_cents = int(amount * 100)
        
        payload = {
            "request": {
                "amount": amount_cents,
                "currency": currency,
                "description": description,
                "tracking_id": order_id,
                "test": self.test_mode,
                "credit_card": {
                    "token": card_token
                },
                "customer": {
                    "email": email
                }
            }
        }

        async with aiohttp.ClientSession(auth=self._auth) as session:
            try:
                async with session.post(gateway_url, json=payload) as response:
                    data = await response.json()
                    transaction = data.get("transaction", {})
                    # Статус успешной оплаты: successful
                    if response.status in (200, 201) and transaction.get("status") == "successful":
                        return True, transaction
                    else:
                        message = transaction.get("message") or "Unknown error"
                        return False, message
            except Exception as e:
                logger.error(f"BePaid recurrent charge failed: {e}")
                return False, str(e)
