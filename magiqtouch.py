import argparse
import json
import random
import string
import time
import ssl
import socket
import asyncio
import logging
import aiobotocore
import threading
from mandate import Cognito
from datetime import datetime
from botocore.errorfactory import BaseClientExceptions
from pathlib import Path

import aiohttp
from AWSIoTPythonSDK.MQTTLib import AWSIoTMQTTClient

# from AWSIoTPythonSDK.core.protocol.connection.cores import SecuredWebSocketCore
# from AWSIoTPythonSDK.core.protocol.connection.alpn import SSLContextBuilder
# from pycognito.aws_srp import AWSSRP
# import asyncio_mqtt

from structures import RemoteStatus, RemoteAccessRequest

cognitoIdentityPoolID = "ap-southeast-2:0ed20c23-4af8-4408-86fc-b78689a5c7a7"

host = "ab7hzia9uew8g-ats.iot.ap-southeast-2.amazonaws.com"

AWS_REGION = "ap-southeast-2"
AWS_USER_POOL_ID = "ap-southeast-2_uw5VVNlib"
AWS_POOL_ID = "ap-southeast-2:0ed20c23-4af8-4408-86fc-b78689a5c7a7"
AWS_PROVIDER_NAME = "cognito-idp.ap-southeast-2.amazonaws.com/ap-southeast-2_uw5VVNlib"
appId = "4e662b6273004a6c9a0797efae6fbb73"
cognito_userpool_client_id = "6e1lu9fchv82uefiarsp0290v9"
AWS_CLIENT_ID = "6e1lu9fchv82uefiarsp0290v9"
AWS_POOL_NAME = "uw5VVNlib"
STATIC_WEBSITE_ENDPOINT = (
    "http://magiqtouch-iot-websites.s3-website-ap-southeast-2.amazonaws.com/"
)

WebServiceURL = "https://57uh36mbv1.execute-api.ap-southeast-2.amazonaws.com/api/"
ApiUrl = (
    "https://57uh36mbv1.execute-api.ap-southeast-2.amazonaws.com"
    + "/api/loadmobiledevice"
)

_LOGGER = logging.getLogger("magiqtouch")

# from https://docs.aws.amazon.com/iot/latest/developerguide/server-authentication.html
rootCAPath = Path(__file__).parent / "SFSRootCAG2.pem"

# # MQTT logging
# logger = logging.getLogger("AWSIoTPythonSDK.core")
# logger.setLevel(logging.DEBUG)
# streamHandler = logging.StreamHandler()
# formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
# streamHandler.setFormatter(formatter)
# logger.addHandler(streamHandler)

# from aiobotocore import credentials

# class AioCredentialResolver(credentials.CredentialResolver):
#     async def load_credentials(self):
#         return None

# credentials.AioCredentialResolver = AioCredentialResolver


class MagiQtouch_Driver:
    def __init__(self, user, password):
        self._password = password
        self._user = user

        self._AccessToken = None
        self._RefreshToken = None
        self._IdToken = None

        self._IdentityId = None

        self._AccessKeyId = None
        self._SecretKey = None
        self._SessionToken = None

        self._mqtt_client_id = None

        self.current_state: RemoteStatus = RemoteStatus()
        self.current_state_just_updated: RemoteStatus = RemoteStatus()
        self._update_listener = None
        self._update_listener_override = None

        self.logged_in = False

    async def login(self):
        _LOGGER.info("Logging in...")
        try:
            ## First, login to cognito with MagiqTouch user/pass
            cog = Cognito(
                user_pool_id=AWS_USER_POOL_ID,
                client_id=cognito_userpool_client_id,
                user_pool_region=AWS_REGION,
                username=self._user,
                access_key='AKIAIOSFODNN7EXAMPLE',
                secret_key='wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'
            )

            await cog.authenticate(self._password)
        except Exception as ex:
            if "UserNotFoundException" in str(ex) or "NotAuthorizedException" in str(
                ex
            ):
                _LOGGER.exception("Error during auth", ex)
                return False
            raise

        self._AccessToken = cog.access_token
        self._RefreshToken = cog.refresh_token
        self._IdToken = cog.id_token

        session = aiobotocore.get_session()
        async with session.create_client(
            "cognito-identity", region_name=AWS_REGION
        ) as identity:
            creds = await identity.get_id(
                IdentityPoolId=AWS_POOL_ID, Logins={AWS_PROVIDER_NAME: self._IdToken}
            )
            self._IdentityId = creds["IdentityId"]

            ident = await identity.get_credentials_for_identity(
                IdentityId=self._IdentityId, Logins={AWS_PROVIDER_NAME: self._IdToken}
            )

            self._AccessKeyId = ident["Credentials"]["AccessKeyId"]
            self._SecretKey = ident["Credentials"]["SecretKey"]
            self._SessionToken = ident["Credentials"]["SessionToken"]

            _LOGGER.debug("Login Expiration:", ident["Credentials"]["Expiration"])

        ## Enable custom policy for user (copied from official app)
        credentials = dict(
            aws_access_key_id=self._AccessKeyId,
            aws_secret_access_key=self._SecretKey,
            aws_session_token=self._SessionToken,
        )
        async with session.create_client(
            "iot", region_name=AWS_REGION, **credentials
        ) as iot:
            _ = await iot.attach_policy(
                policyName="SeelyIoTPolicy", target=self._IdentityId
            )

        ## Get MACADDRESS
        async with aiohttp.ClientSession() as http:
            async with http.get(
                ApiUrl, headers={"Authorization": self._IdToken}
            ) as rsp:
                self._mac_address = (await rsp.json())[0]["MacAddressId"]
        _LOGGER.debug("MAC:", self._mac_address)

        self.logged_in = True
        return True

    async def logout(self):
        # TODO
        pass

    ## MQTT

    @property
    def mqtt_publish_topic(self):
        return "SeeleyIoT/{0}/MobileRequest".format(self._mac_address)

    @property
    def mqtt_subscribe_topic(self):
        return "SeeleyIoT/{0}/MobileRealTime".format(self._mac_address)

    @property
    def mqtt_subscribe_fw_topic(self):
        return "SeeleyIoT/{0}/FirmwareUpdate".format(self._mac_address)

    @property
    def mqtt_client_id(self):
        if not self._mqtt_client_id:
            ## Create random mqtt client id (copied from the official app)
            self._mqtt_client_id = "MagIQ0" + "".join(
                random.choices(string.digits, k=16)
            )
        return self._mqtt_client_id

    async def a_mqtt_connect(self):
        port = 443
        c = asyncio_mqtt.Client(host, port=port, client_id=self.mqtt_client_id)
        c._loop = asyncio.SelectorEventLoop()

        from paho import mqtt

        magiq = self

        class WebsocketWrapper(mqtt.client.WebsocketWrapper):
            def _do_handshake(self, extra_headers):
                rawSSL = ssl.wrap_socket(
                    self._socket, ca_certs=str(rootCAPath), cert_reqs=ssl.CERT_REQUIRED
                )  # Add server certificate verification
                rawSSL.setblocking(0)  # Non-blocking socket
                rawSSL.do_handshake()
                self._socket = SecuredWebSocketCore(
                    rawSSL,
                    self._host,
                    self._port,
                    magiq._AccessKeyId,
                    magiq._SecretKey,
                    magiq._SessionToken,
                )  # Override the _ssl socket
                self._ssl = True
                # self._socket = self._ssl.getSSLSocket()
                self.connected = True

            def setblocking(self, flag):
                return self._socket.getSSLSocket().setblocking(flag)

            def setsockopt(self, *args):
                # asyncio_mqtt tries to use this, doesn't exist for websocket
                pass

            def fileno(self):
                return self._socket.getSSLSocket().fileno()

            def _send_impl(self, data):
                return self._socket.write(data)

            def _recv_impl(self, length):
                return self._socket.read(length)

        mqtt.client.WebsocketWrapper = WebsocketWrapper

        c._client._transport = "websockets"

        # sock = socket.create_connection((host, port))
        # rawSSL = ssl.wrap_socket(sock, ca_certs=rootCAPath, cert_reqs=ssl.CERT_REQUIRED)  # Add server certificate verification
        # rawSSL.setblocking(0)  # Non-blocking socket
        # self._ssl = SecuredWebSocketCore(rawSSL, host, port, self._AccessKeyId, self._SecretKey, self._SessionToken)  # Override the _ssl socket
        # sock = self._ssl.getSSLSocket()

        async with c as client:
            async with client.filtered_messages(self.mqtt_subscribe_topic) as messages:
                await client.subscribe(self.mqtt_subscribe_topic)
                async for message in messages:
                    print(message.payload.decode())

    def mqtt_connect(self):
        if not self.logged_in:
            raise ValueError("Not logged in")
        # Init AWSIoTMQTTClient
        self._mqtt_client = AWSIoTMQTTClient(self.mqtt_client_id, useWebsocket=True)

        # AWSIoTMQTTClient configuration
        self._mqtt_client.configureEndpoint(host, 443)
        self._mqtt_client.configureCredentials(rootCAPath)
        self._mqtt_client.configureIAMCredentials(
            self._AccessKeyId, self._SecretKey, self._SessionToken
        )
        self._mqtt_client.configureAutoReconnectBackoffTime(1, 32, 20)
        self._mqtt_client.configureOfflinePublishQueueing(
            -1
        )  # Infinite offline Publish queueing
        self._mqtt_client.configureDrainingFrequency(2)  # Draining: 2 Hz
        self._mqtt_client.configureConnectDisconnectTimeout(20)  # 10 sec
        self._mqtt_client.configureMQTTOperationTimeout(10)  # 5 sec

        # def myOnMessageCallback(message):
        #     print("myOnMessageCallback:", message)
        #
        # self._mqtt_client.onMessage = myOnMessageCallback

        # Connect and subscribe to AWS IoT
        self._mqtt_client.connect()
        _LOGGER.debug("MQTT Connected")

        self._mqtt_client.subscribe(
            self.mqtt_subscribe_topic, 1, self._mqtt_response_handler
        )

        self.refresh_state()

    def set_listener(self, listener):
        self._update_listener = listener

    def _mqtt_response_handler(self, client, userdata, message):
        if message.topic == self.mqtt_subscribe_topic:
            try:
                data = json.loads(message.payload)
                new_state = RemoteStatus()
                new_state.__update__(data)
                self.current_state_just_updated = new_state
                if self._update_listener_override:
                    _LOGGER.warn("State watching: %s" % new_state)
                    self._update_listener_override()
                    return
                elif self._update_listener:
                    _LOGGER.warn("State updated: %s" % new_state)
                    self._update_listener()
                self.current_state = new_state
            except ValueError as ex:
                _LOGGER.exception("Failed to parse current state", ex)
        else:
            _LOGGER.warn("Received an unexpected message: ")
            _LOGGER.warn(message.payload)
            _LOGGER.warn("from topic: ")
            _LOGGER.warn(message.topic)
            _LOGGER.warn("--------------\n\n")

    def refresh_state(self):
        self._mqtt_client.publish(
            self.mqtt_publish_topic,
            json.dumps(
                {
                    "SerialNo": self._mac_address,
                    "Status": 1,
                }
            ),
            1,
        )

    def new_remote_props(self, state=None):
        state = state or self.current_state
        data = RemoteAccessRequest()
        data.SerialNo = state.MacAddressId
        data.TimeRequest = datetime.now().astimezone().isoformat()
        data.StandBy = 0 if state.SystemOn else 1
        data.EvapCRunning = state.EvapCRunning
        data.CTemp = state.CTemp
        data.CFanSpeed = state.CFanSpeed
        data.CFanOnly = state.CFanOnlyOrCool
        data.CThermosOrFan = 0 if state.CFanOnlyOrCool else state.FanOrTempControl
        data.HRunning = state.HRunning
        data.HTemp = state.HTemp
        data.HFanSpeed = 1
        data.HFanOnly = state.HFanOnly
        data.FAOCRunning = state.FAOCRunning
        data.FAOCTemp = state.FAOCTemp
        data.IAOCRunning = state.IAOCRunning
        data.IAOCTemp = state.IAOCSetTemp
        data.OnOffZone1 = 1
        data.TempZone1 = state.SetTempZone1
        data.Override1 = state.ProgramModeOverriddenZone1

        # Could/should do a get fw version pub/sub to have values to fill these with
        # CC3200FW_Major = state.CC3200FW_Major
        # CC3200FW_Minor = state.CC3200FW_Minor
        # STM32FW_Major = state.STM32FW_Major
        # STM32FW_Minor = state.STM32FW_Minor

        return data

    def _send_remote_props(self, data=None, checker=None):
        data = data or self.new_remote_props()
        json = data.__json__(indent=0).replace("\n", "")
        try:
            update_lock = threading.Lock()
            if checker:
                update_lock.acquire()
                def override_listener():
                    if checker(self.current_state):
                        update_lock.release()


                self._update_listener_override = override_listener

            self._mqtt_client.publish(self.mqtt_publish_topic, json, 1)
            _LOGGER.warn("Sent: %s" % json)

            if checker:
                # Wait for expected response
                update_lock.acquire(timeout=6)

        except Exception as ex:
            _LOGGER.exception("Failed to publish", ex)
            raise
        finally:
            self._update_listener_override = None

    def set_off(self):
        self.current_state.SystemOn = 0
        checker = lambda state: state.SystemOn == 0
        self._send_remote_props(checker=checker)

    def set_fan_only(self):
        self.current_state.SystemOn = 1
        self.current_state.CFanOnlyOrCool = 1
        checker = lambda state: state.CFanOnlyOrCool == 1 and state.SystemOn == 1
        self._send_remote_props(checker=checker)

    def set_cooling_by_temperature(self):
        self.set_cooling(temp_mode=1)

    def set_cooling_by_speed(self):
        self.set_cooling(temp_mode=0)

    def set_cooling(self, temp_mode):
        temp_mode = 1 if temp_mode else 0
        self.current_state.FanOrTempControl = temp_mode
        self.current_state.SystemOn = 1
        self.current_state.CFanOnlyOrCool = 0
        def checker(state):
            return state.CFanOnlyOrCool == 0 and \
                   state.FanOrTempControl == temp_mode and \
                   state.SystemOn == 1
        self._send_remote_props(checker=checker)

    def set_current_speed(self, speed):
        self.current_state.CFanSpeed = speed
        expected = 0 if self.current_state.CFanSpeed == 0 else speed
        checker = lambda state: state.CFanSpeed == expected
        self._send_remote_props(checker=checker)

    def set_temperature(self, new_temp):
        self.current_state.CTemp = new_temp
        self.current_state.HTemp = new_temp
        self.current_state.FAOCTemp = new_temp
        self.current_state.IAOCSetTemp = new_temp
        self.current_state.SetTempZone1 = new_temp
        checker = lambda state: state.CTemp == new_temp
        self._send_remote_props(checker=checker)


def main():
    # Read in command-line parameters
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-e",
        "--email",
        action="store",
        required=True,
        dest="email",
        help="Your Magiqtouch login email",
    )
    parser.add_argument(
        "-p",
        "--password",
        action="store",
        required=True,
        dest="password",
        help="Your Magiqtouch login password",
    )

    args = parser.parse_args()
    user = args.email
    password = args.password

    logging.basicConfig(format='%(levelname)s:%(message)s', level=logging.DEBUG)

    m = MagiQtouch_Driver(user=user, password=password)

    loop = asyncio.get_event_loop()
    loop.run_until_complete(m.login())
    # loop.run_until_complete(m.a_mqtt_connect())

    m.mqtt_connect()
    m.refresh_state()

    while not m.current_state:
        time.sleep(1)
    print(m.current_state)

    def handler1():
        s = m.current_state_just_updated
        _LOGGER.debug("handler1 on=%d,fanMode=%d,evapRunning=%d, tempC=%d, setTempC=%d,setFanSpeed=%d " % (s.SystemOn, 1-s.FanOrTempControl, s.EvapCRunning, s.InternalTemp, s.CTemp, s.CFanSpeed))

    m.set_listener(handler1)

    while True:
        time.sleep(1)

if __name__ == "__main__":
    main()
