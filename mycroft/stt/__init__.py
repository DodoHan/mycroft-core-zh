# Copyright 2017 Mycroft AI Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import re
import json
import os
from abc import ABCMeta, abstractmethod
from requests import post, put, exceptions
from speech_recognition import Recognizer
from queue import Queue
from threading import Thread

from mycroft.api import STTApi, HTTPError
from mycroft.configuration import Configuration
from mycroft.util.log import LOG
from mycroft.util.plugins import load_plugin
from mycroft.util.file_utils import get_temp_path


class STT(metaclass=ABCMeta):
    """STT Base class, all STT backends derive from this one. """

    def __init__(self):
        config_core = Configuration.get()
        self.lang = str(self.init_language(config_core))
        config_stt = config_core.get("stt", {})
        self.config = config_stt.get(config_stt.get("module"), {})
        self.credential = self.config.get("credential", {})
        self.recognizer = Recognizer()
        self.can_stream = False

    @staticmethod
    def init_language(config_core):
        """Helper method to get language code from Mycroft config."""
        lang = config_core.get("lang", "en-US")
        langs = lang.split("-")
        if len(langs) == 2:
            return langs[0].lower() + "-" + langs[1].upper()
        return lang

    @abstractmethod
    def execute(self, audio, language=None):
        """Implementation of STT functionallity.

        This method needs to be implemented by the derived class to implement
        the specific STT engine connection.

        The method gets passed audio and optionally a language code and is
        expected to return a text string.

        Args:
            audio (AudioData): audio recorded by mycroft.
            language (str): optional language code

        Returns:
            str: parsed text
        """


class TokenSTT(STT, metaclass=ABCMeta):

    def __init__(self):
        super(TokenSTT, self).__init__()
        self.token = str(self.credential.get("token"))


class GoogleJsonSTT(STT, metaclass=ABCMeta):

    def __init__(self):
        super(GoogleJsonSTT, self).__init__()
        self.json_credentials = json.dumps(self.credential.get("json"))


class BasicSTT(STT, metaclass=ABCMeta):

    def __init__(self):
        super(BasicSTT, self).__init__()
        self.username = str(self.credential.get("username"))
        self.password = str(self.credential.get("password"))


class KeySTT(STT, metaclass=ABCMeta):

    def __init__(self):
        super(KeySTT, self).__init__()
        self.id = str(self.credential.get("client_id"))
        self.key = str(self.credential.get("client_key"))


class GoogleSTT(TokenSTT):

    def __init__(self):
        super(GoogleSTT, self).__init__()

    def execute(self, audio, language=None):
        self.lang = language or self.lang
        return self.recognizer.recognize_google(audio, self.token, self.lang)


class GoogleCloudSTT(GoogleJsonSTT):

    def __init__(self):
        super(GoogleCloudSTT, self).__init__()
        # override language with module specific language selection
        self.lang = self.config.get('lang') or self.lang

    def execute(self, audio, language=None):
        self.lang = language or self.lang
        return self.recognizer.recognize_google_cloud(audio,
                                                      self.json_credentials,
                                                      self.lang)


class WITSTT(TokenSTT):

    def __init__(self):
        super(WITSTT, self).__init__()

    def execute(self, audio, language=None):
        LOG.warning("WITSTT language should be configured at wit.ai settings.")
        return self.recognizer.recognize_wit(audio, self.token)


class IBMSTT(TokenSTT):
    """
        IBM Speech to Text
        Enables IBM Speech to Text access using API key. To use IBM as a
        service provider, it must be configured locally in your config file. An
        IBM Cloud account with Speech to Text enabled is required (limited free
        tier may be available). STT config should match the following format:

        "stt": {
            "module": "ibm",
            "ibm": {
                "credential": {
                    "token": "YOUR_API_KEY"
                },
                "url": "URL_FROM_SERVICE"
            }
        }
    """

    def __init__(self):
        super(IBMSTT, self).__init__()

    def execute(self, audio, language=None):
        if not self.token:
            raise ValueError('API key (token) for IBM Cloud is not defined.')

        url_base = self.config.get('url', '')
        if not url_base:
            raise ValueError('URL for IBM Cloud is not defined.')
        url = url_base + '/v1/recognize'

        self.lang = language or self.lang
        supported_languages = [
            'ar-AR', 'pt-BR', 'zh-CN', 'nl-NL', 'en-GB', 'en-US', 'fr-FR',
            'de-DE', 'it-IT', 'ja-JP', 'ko-KR', 'es-AR', 'es-ES', 'es-CL',
            'es-CO', 'es-MX', 'es-PE'
        ]
        if self.lang not in supported_languages:
            raise ValueError('Unsupported language "{}" for IBM STT.'.format(
                self.lang))

        audio_model = 'BroadbandModel'
        if audio.sample_rate < 16000 and not self.lang == 'ar-AR':
            audio_model = 'NarrowbandModel'

        params = {
            'model': '{}_{}'.format(self.lang, audio_model),
            'profanity_filter': 'false'
        }
        headers = {
            'Content-Type': 'audio/x-flac',
            'X-Watson-Learning-Opt-Out': 'true'
        }

        response = post(url,
                        auth=('apikey', self.token),
                        headers=headers,
                        data=audio.get_flac_data(),
                        params=params)

        if response.status_code == 200:
            result = json.loads(response.text)
            if result.get('error_code') is None:
                if ('results' not in result or len(result['results']) < 1 or 'alternatives' not in result['results'][0]):
                    raise Exception(
                        'Transcription failed. Invalid or empty results.')
                transcription = []
                for utterance in result['results']:
                    if 'alternatives' not in utterance:
                        raise Exception(
                            'Transcription failed. Invalid or empty results.')
                    for hypothesis in utterance['alternatives']:
                        if 'transcript' in hypothesis:
                            transcription.append(hypothesis['transcript'])
                return '\n'.join(transcription)
        elif response.status_code == 401:  # Unauthorized
            raise Exception('Invalid API key for IBM Cloud.')
        else:
            raise Exception(
                'Request to IBM Cloud failed. Code: {} Body: {}'.format(
                    response.status_code, response.text))


class YandexSTT(STT):
    """
        Yandex SpeechKit STT
        To use create service account with role 'editor' in your cloud folder,
        create API key for account and add it to local mycroft.conf file.
        The STT config will look like this:

        "stt": {
            "module": "yandex",
            "yandex": {
                "lang": "en-US",
                "credential": {
                    "api_key": "YOUR_API_KEY"
                }
            }
        }
    """

    def __init__(self):
        super(YandexSTT, self).__init__()
        self.lang = self.config.get('lang') or self.lang
        self.api_key = self.credential.get("api_key")
        if self.api_key is None:
            raise ValueError("API key for Yandex STT is not defined")

    def execute(self, audio, language=None):
        self.lang = language or self.lang
        if self.lang not in ["en-US", "ru-RU", "tr-TR"]:
            raise ValueError("Unsupported language '{}' for Yandex STT".format(
                self.lang))

        # Select sample rate based on source sample rate
        # and supported sample rate list
        supported_sample_rates = [8000, 16000, 48000]
        sample_rate = audio.sample_rate
        if sample_rate not in supported_sample_rates:
            for supported_sample_rate in supported_sample_rates:
                if audio.sample_rate < supported_sample_rate:
                    sample_rate = supported_sample_rate
                    break
            if sample_rate not in supported_sample_rates:
                sample_rate = supported_sample_rates[-1]

        raw_data = audio.get_raw_data(convert_rate=sample_rate,
                                      convert_width=2)

        # Based on https://cloud.yandex.com/docs/speechkit/stt#request
        url = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"
        headers = {"Authorization": "Api-Key {}".format(self.api_key)}
        params = "&".join([
            "lang={}".format(self.lang), "format=lpcm",
            "sampleRateHertz={}".format(sample_rate)
        ])

        response = post(url + "?" + params, headers=headers, data=raw_data)
        if response.status_code == 200:
            result = json.loads(response.text)
            if result.get("error_code") is None:
                return result.get("result")
        elif response.status_code == 401:  # Unauthorized
            raise Exception("Invalid API key for Yandex STT")
        else:
            raise Exception(
                "Request to Yandex STT failed: code: {}, body: {}".format(
                    response.status_code, response.text))


def requires_pairing(func):
    """Decorator kicking of pairing sequence if client is not allowed access.

    Checks the http status of the response if an HTTP error is recieved. If
    a 401 status is detected returns "pair my device" to trigger the pairing
    skill.
    """

    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except HTTPError as e:
            if e.response.status_code == 401:
                LOG.warning('Access Denied at mycroft.ai')
                # phrase to start the pairing process
                return 'pair my device'
            else:
                raise

    return wrapper


class MycroftSTT(STT):
    """Default mycroft STT."""

    def __init__(self):
        # mycroft-core-zh: todo, pairing doesn't support. add the config into mycroft.conf.
        if True:
            LOG.error(
                'MycroftSTT is not supported for mycroft-core-zh, Pairing(connect with web backend server such as https://api.mycroft.ai) is not supported by mycroft-core-zh'
            )
        super(MycroftSTT, self).__init__()
        self.api = STTApi("stt")

    @requires_pairing
    def execute(self, audio, language=None):
        self.lang = language or self.lang
        try:
            return self.api.stt(audio.get_flac_data(convert_rate=16000),
                                self.lang, 1)[0]
        except Exception:
            return self.api.stt(audio.get_flac_data(), self.lang, 1)[0]


class MycroftDeepSpeechSTT(STT):
    """Mycroft Hosted DeepSpeech"""

    def __init__(self):
        # mycroft-core-zh: todo, pairing doesn't support. add the config into mycroft.conf.
        if True:
            LOG.error(
                'MycroftDeepSpeechSTT is not supported for mycroft-core-zh, Pairing(connect with web backend server such as https://api.mycroft.ai) is not supported by mycroft-core-zh'
            )
        super(MycroftDeepSpeechSTT, self).__init__()
        self.api = STTApi("deepspeech")

    @requires_pairing
    def execute(self, audio, language=None):
        language = language or self.lang
        if not language.startswith("en"):
            raise ValueError("Deepspeech is currently english only")
        return self.api.stt(audio.get_wav_data(), self.lang, 1)


class DeepSpeechServerSTT(STT):
    """
        STT interface for the deepspeech-server:
        https://github.com/MainRo/deepspeech-server
        use this if you want to host DeepSpeech yourself
    """

    def __init__(self):
        super(DeepSpeechServerSTT, self).__init__()

    def execute(self, audio, language=None):
        language = language or self.lang
        response = post(self.config.get("uri"), data=audio.get_wav_data())
        return response.text


class StreamThread(Thread, metaclass=ABCMeta):
    """ABC class to be used with StreamingSTT class implementations.

    This class reads audio chunks from a queue and sends it to a parsing
    STT engine.

    Args:
        queue (Queue): Input Queue
        language (str): language code for the current language.
    """

    def __init__(self, queue, language):
        super().__init__()
        self.language = language
        self.queue = queue
        self.text = None

    def _get_data(self):
        """Generator reading audio data from queue."""
        while True:
            d = self.queue.get()
            if d is None:
                break
            yield d
            self.queue.task_done()

    def run(self):
        """Thread entry point."""
        return self.handle_audio_stream(self._get_data(), self.language)

    @abstractmethod
    def handle_audio_stream(self, audio, language):
        """Handling of audio stream.

        Needs to be implemented by derived class to process audio data and
        optionally update `self.text` with the current hypothesis.

        Argumens:
            audio (bytes): raw audio data.
            language (str): language code for the current session.
        """


class StreamingSTT(STT, metaclass=ABCMeta):
    """ABC class for threaded streaming STT implemenations."""

    def __init__(self):
        super().__init__()
        self.stream = None
        self.can_stream = True

    def stream_start(self, language=None):
        """Indicate start of new audio stream.

        This creates a new thread for handling the incomming audio stream as
        it's collected by Mycroft.

        Args:
            language (str): optional language code for the new stream.
        """
        self.stream_stop()
        language = language or self.lang
        self.queue = Queue()
        self.stream = self.create_streaming_thread()
        self.stream.start()

    def stream_data(self, data):
        """Receiver of audio data.

        Args:
            data (bytes): raw audio data.
        """
        self.queue.put(data)

    def stream_stop(self):
        """Indicate that the audio stream has ended.

        This will tear down the processing thread and collect the result

        Returns:
            str: parsed text
        """
        if self.stream is not None:
            self.queue.put(None)
            self.stream.join()

            text = self.stream.text

            self.stream = None
            self.queue = None
            return text
        return None

    def execute(self, audio, language=None):
        """End the parsing thread and collect data."""
        return self.stream_stop()

    @abstractmethod
    def create_streaming_thread(self):
        """Create thread for parsing audio chunks.

        This method should be implemented by the derived class to return an
        instance derived from StreamThread to handle the audio stream and
        send it to the STT engine.

        Returns:
            StreamThread: Thread to handle audio data.
        """


class DeepSpeechStreamThread(StreamThread):

    def __init__(self, queue, language, url):
        if not language.startswith("en"):
            raise ValueError("Deepspeech is currently english only")
        super().__init__(queue, language)
        self.url = url

    def handle_audio_stream(self, audio, language):
        self.response = post(self.url, data=audio, stream=True)
        self.text = self.response.text if self.response else None
        return self.text


class DeepSpeechStreamServerSTT(StreamingSTT):
    """
        Streaming STT interface for the deepspeech-server:
        https://github.com/JPEWdev/deep-dregs
        use this if you want to host DeepSpeech yourself
        STT config will look like this:

        "stt": {
            "module": "deepspeech_stream_server",
            "deepspeech_stream_server": {
                "stream_uri": "http://localhost:8080/stt?format=16K_PCM16"
        ...
    """

    def create_streaming_thread(self):
        self.queue = Queue()
        return DeepSpeechStreamThread(self.queue, self.lang,
                                      self.config.get('stream_uri'))


class GoogleStreamThread(StreamThread):

    def __init__(self, queue, lang, client, streaming_config):
        super().__init__(queue, lang)
        self.client = client
        self.streaming_config = streaming_config

    def handle_audio_stream(self, audio, language):
        req = (types.StreamingRecognizeRequest(audio_content=x) for x in audio)
        responses = self.client.streaming_recognize(self.streaming_config, req)
        for res in responses:
            if res.results and res.results[0].is_final:
                self.text = res.results[0].alternatives[0].transcript
        return self.text


class GoogleCloudStreamingSTT(StreamingSTT):
    """
        Streaming STT interface for Google Cloud Speech-To-Text
        To use pip install google-cloud-speech and add the
        Google API key to local mycroft.conf file. The STT config
        will look like this:

        "stt": {
            "module": "google_cloud_streaming",
            "google_cloud_streaming": {
                "credential": {
                    "json": {
                        # Paste Google API JSON here
        ...

    """

    def __init__(self):
        global SpeechClient, types, enums, Credentials
        from google.cloud.speech import SpeechClient, types, enums
        from google.oauth2.service_account import Credentials

        super(GoogleCloudStreamingSTT, self).__init__()
        # override language with module specific language selection
        self.language = self.config.get('lang') or self.lang
        credentials = Credentials.from_service_account_info(
            self.credential.get('json'))

        self.client = SpeechClient(credentials=credentials)
        recognition_config = types.RecognitionConfig(
            encoding=enums.RecognitionConfig.AudioEncoding.LINEAR16,
            sample_rate_hertz=16000,
            language_code=self.language,
            model='command_and_search',
            max_alternatives=1,
        )
        self.streaming_config = types.StreamingRecognitionConfig(
            config=recognition_config,
            interim_results=True,
            single_utterance=True,
        )

    def create_streaming_thread(self):
        self.queue = Queue()
        return GoogleStreamThread(self.queue, self.language, self.client,
                                  self.streaming_config)


class KaldiSTT(STT):

    def __init__(self):
        super(KaldiSTT, self).__init__()

    def execute(self, audio, language=None):
        language = language or self.lang
        response = post(self.config.get("uri"), data=audio.get_wav_data())
        return self.get_response(response)

    def get_response(self, response):
        try:
            hypotheses = response.json()["hypotheses"]
            return re.sub(r'\s*\[noise\]\s*', '', hypotheses[0]["utterance"])
        except Exception:
            return None


class BingSTT(TokenSTT):

    def __init__(self):
        super(BingSTT, self).__init__()

    def execute(self, audio, language=None):
        self.lang = language or self.lang
        return self.recognizer.recognize_bing(audio, self.token, self.lang)


class HoundifySTT(KeySTT):

    def __init__(self):
        super(HoundifySTT, self).__init__()

    def execute(self, audio, language=None):
        self.lang = language or self.lang
        return self.recognizer.recognize_houndify(audio, self.id, self.key)


class GoVivaceSTT(TokenSTT):

    def __init__(self):
        super(GoVivaceSTT, self).__init__()
        self.default_uri = "https://services.govivace.com:49149/telephony"

        if not self.lang.startswith("en") and not self.lang.startswith("es"):
            LOG.error("GoVivace STT only supports english and spanish")
            raise NotImplementedError

    def execute(self, audio, language=None):
        url = self.config.get("uri", self.default_uri) + "?key=" + \
              self.token + "&action=find&format=8K_PCM16&validation_string="
        response = put(url, data=audio.get_wav_data(convert_rate=8000))
        return self.get_response(response)

    def get_response(self, response):
        return response.json()["result"]["hypotheses"][0]["transcript"]


def load_stt_plugin(module_name):
    """Wrapper function for loading stt plugin.

    Args:
        module_name (str): Mycroft stt module name from config
    Returns:
        class: STT plugin class
    """
    return load_plugin('mycroft.plugin.stt', module_name)


class BaiduSTT(STT):

    def __init__(self):
        from aip import AipSpeech
        LOG.info('[Flow Learning] in BaiduSTT.__init__')
        super(BaiduSTT, self).__init__()
        self.lang = self.config['lang']
        LOG.info('[Flow Learning] self.config = ' + str(self.config))
        self.sample_rate = 16000 if self.config[
            'sample_rate'] is None else self.config['sample_rate']
        self.per = 0 if self.config['per'] is None else self.config['per']
        self.dev_pid = self.config['dev_pid']
        self.format = 'pcm'
        self.client = AipSpeech(self.config['appid'],
                                self.credential['api_key'],
                                self.credential['secret_key'])

    def execute(self, audio, language=None):
        LOG.info('is about to send voice to Baidu!')
        waveData = audio.get_raw_data(convert_rate=self.sample_rate)
        res = self.client.asr(waveData, self.format, self.sample_rate, {
            'dev_pid': self.dev_pid,
        })
        if res['err_no'] == 0:
            LOG.info('Speech is recognized:{}'.format(res['result']))
            return ''.join(res['result'])
        else:
            LOG.info('Error during Speech recognition: {}'.format(
                res['err_msg']))
            return ''


# Sample code to use miscrosoft speech recognization.
# https://gitee.com/AndroidYaoDan/cognitive-services-speech-sdk/tree/master/samples/python/console
class MicrosoftSTT(STT):

    def __init__(self):
        import azure.cognitiveservices.speech as speechsdk
        LOG.info('[Flow Learning] in MicrosoftSTT.__init__')
        super(MicrosoftSTT, self).__init__()
        self.speechsdk = speechsdk
        self.speech_config = speechsdk.SpeechConfig(
            subscription=self.credential['key'],
            region=self.credential['region'])
        self.lang = self.config['lang']
        self.sample_rate = self.config['sample_rate']

    def execute(self, audio, language=None):
        LOG.info('is about to send voice to Microsoft!')
        waveData = audio.get_wav_data(convert_rate=self.sample_rate)
        some_bytes = waveData

        # Open file in binary write mode
        # It goes to /tmp/mycroft
        file_name = "microsoft_stt_tmp.wav"
        file_path = get_temp_path("mycroft", file_name)
        LOG.info("Temp voice file_name for Microsoft STT == " + file_path)
        binary_file = open(file_path, "wb")

        # Write bytes to file
        binary_file.write(some_bytes)
        # Close file
        binary_file.close()

        audio_config = self.speechsdk.audio.AudioConfig(filename=file_path)
        # Creates a speech recognizer using a file as audio input, also specify the speech language
        speech_recognizer = self.speechsdk.SpeechRecognizer(
            speech_config=self.speech_config,
            language=self.lang,
            audio_config=audio_config)

        result = speech_recognizer.recognize_once()

        # Check the result
        if result.reason == self.speechsdk.ResultReason.RecognizedSpeech:
            LOG.info("Recognized: {}".format(result.text))
            return result.text
        elif result.reason == self.speechsdk.ResultReason.NoMatch:
            LOG.info("No speech could be recognized: {}".format(
                result.no_match_details))
        elif result.reason == self.speechsdk.ResultReason.Canceled:
            cancellation_details = result.cancellation_details
            LOG.info("Speech Recognition canceled: {}".format(
                cancellation_details.reason))
            if cancellation_details.reason == self.speechsdk.CancellationReason.Error:
                LOG.info("Error details: {}".format(
                    cancellation_details.error_details))


class STTFactory:
    CLASSES = {
        "mycroft": MycroftSTT,
        "google": GoogleSTT,
        "google_cloud": GoogleCloudSTT,
        "google_cloud_streaming": GoogleCloudStreamingSTT,
        "wit": WITSTT,
        "ibm": IBMSTT,
        "kaldi": KaldiSTT,
        "bing": BingSTT,
        "govivace": GoVivaceSTT,
        "houndify": HoundifySTT,
        "deepspeech_server": DeepSpeechServerSTT,
        "deepspeech_stream_server": DeepSpeechStreamServerSTT,
        "mycroft_deepspeech": MycroftDeepSpeechSTT,
        "yandex": YandexSTT,
        "baidu": BaiduSTT,
        "microsoft": MicrosoftSTT,
    }

    @staticmethod
    def create():
        try:
            config = Configuration.get().get("stt", {})
            module = config.get("module", "mycroft")
            LOG.info("module of stt is " + module)
            if module in STTFactory.CLASSES:
                LOG.info("module is in STTFactory.CLASSES: module ==" + module)
                clazz = STTFactory.CLASSES[module]
            else:
                clazz = load_stt_plugin(module)
                LOG.info('Loaded the STT plugin {}'.format(module))
            return clazz()
        except Exception:
            # The STT backend failed to start. Report it and fall back to
            # default.
            LOG.exception('The selected STT backend could not be loaded, '
                          'falling back to default...')
            if module != 'mycroft':
                return MycroftSTT()
            else:
                raise
