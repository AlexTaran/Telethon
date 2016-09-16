import platform
from datetime import datetime
from hashlib import md5
from os import path
from mimetypes import guess_extension, guess_type

import utils
import network.authenticator

from errors import *
from network import MtProtoSender, TcpTransport
from parser.markdown_parser import parse_message_entities

# For sending and receiving requests
from tl import MTProtoRequest
from tl import Session

# The Requests and types that we'll be using
from tl.types import \
    PeerUser, PeerChat, PeerChannel, \
    InputPeerUser, InputPeerChat, InputPeerChannel, InputPeerEmpty, \
    InputFile, InputFileLocation, InputMediaUploadedPhoto, InputMediaUploadedDocument, \
    MessageMediaContact, MessageMediaDocument, MessageMediaPhoto, \
    DocumentAttributeAudio, DocumentAttributeFilename, InputDocumentFileLocation

from tl.functions import InvokeWithLayerRequest, InitConnectionRequest
from tl.functions.help import GetConfigRequest
from tl.functions.auth import SendCodeRequest, SignInRequest
from tl.functions.upload import SaveFilePartRequest, GetFileRequest
from tl.functions.messages import GetDialogsRequest, GetHistoryRequest, SendMessageRequest, SendMediaRequest


class TelegramClient:

    # region Initialization

    def __init__(self, session_user_id, api_id, api_hash, version):
        """Initializes the TelegramClient

        :param session_user_id: The ID for the current session. This could be a name, such as "john"
        :param api_id: Your Telegram API ID
        :param api_hash: Your Telegram API Hash
        :param version: The version of your application. Should be a string (i.e., 'x.y')
        """
        if api_id is None or api_hash is None:
            raise PermissionError('Your API ID or Hash are invalid. Please read "Requirements" on README.md')

        self.api_id = api_id
        self.api_hash = api_hash

        self.layer = utils.get_current_layer()
        self.__version__ = version

        self.session = Session.try_load_or_create_new(session_user_id)
        self.transport = TcpTransport(self.session.server_address, self.session.port)

        # These will be set later
        self.dc_options = None
        self.sender = None
        self.phone_code_hashes = {}

    # endregion

    # region Connecting

    def connect(self, reconnect=False):
        """Connects to the Telegram servers, executing authentication if required.
           Note that authenticating to the Telegram servers is not the same as authenticating
           the app, which requires to send a code first."""
        try:
            if not self.session.auth_key or reconnect:
                self.session.auth_key, self.session.time_offset = \
                    network.authenticator.do_authentication(self.transport)

                self.session.save()

            self.sender = MtProtoSender(self.transport, self.session)

            # Now it's time to send an InitConnectionRequest
            # This must always be invoked with the layer we'll be using
            query = InitConnectionRequest(api_id=self.api_id,
                                          device_model=platform.node(),
                                          system_version=platform.system(),
                                          app_version=self.__version__,
                                          lang_code='en',
                                          query=GetConfigRequest())

            result = self.invoke(InvokeWithLayerRequest(layer=self.layer, query=query))

            # We're only interested in the DC options,
            # although many other options are available!
            self.dc_options = result.dc_options
            return True
        except RPCError as error:
            print('Could not stabilise initial connection: {}'.format(error))
            return False

    def reconnect_to_dc(self, dc_id):
        """Reconnects to the specified DC ID. This is automatically called after an InvalidDCError is raised"""
        if self.dc_options is None or not self.dc_options:
            raise ConnectionError("Can't reconnect. Stabilise an initial connection first.")

        dc = next(dc for dc in self.dc_options if dc.id == dc_id)

        self.transport.close()
        self.transport = TcpTransport(dc.ip_address, dc.port)
        self.session.server_address = dc.ip_address
        self.session.port = dc.port
        self.session.save()

        self.connect(reconnect=True)

    def disconnect(self):
        """Disconnects from the Telegram server **and pauses all the spawned threads**"""
        if self.sender:
            self.sender.disconnect()

    # endregion

    # region Telegram requests functions

    def invoke(self, request):
        """Invokes a MTProtoRequest (sends and receives it) and returns its result"""
        if not issubclass(type(request), MTProtoRequest):
            raise ValueError('You can only invoke MtProtoRequests')

        self.sender.send(request)
        self.sender.receive(request)

        return request.result

    # region Authorization requests

    def is_user_authorized(self):
        """Has the user been authorized yet (code request sent and confirmed)?
           Note that this will NOT yield the correct result if the session was revoked by another client!"""
        return self.session.user is not None

    def send_code_request(self, phone_number):
        """Sends a code request to the specified phone number"""
        request = SendCodeRequest(phone_number, self.api_id, self.api_hash)
        completed = False
        while not completed:
            try:
                result = self.invoke(request)
                self.phone_code_hashes[phone_number] = result.phone_code_hash
                completed = True

            except InvalidDCError as error:
                self.reconnect_to_dc(error.new_dc)

    def make_auth(self, phone_number, code):
        """Completes the authorization of a phone number by providing the received code"""
        if phone_number not in self.phone_code_hashes:
            raise ValueError('Please make sure you have called send_code_request first.')

        try:
            request = SignInRequest(phone_number, self.phone_code_hashes[phone_number], code)
            self.sender.send(request)
            self.sender.receive(request)
        except RPCError as error:
            if error.message.startswith('PHONE_CODE_'):
                print(error)
                return False
            else:
                raise error

        # Result is an Auth.Authorization TLObject
        self.session.user = request.result.user
        self.session.save()

        # Now that we're authorized, we can listen for incoming updates
        self.sender.set_listen_for_updates(True)

        return True

    # endregion

    # region Dialogs ("chats") requests

    def get_dialogs(self, count=10, offset_date=None, offset_id=0, offset_peer=InputPeerEmpty()):
        """Returns a tuple of lists ([dialogs], [displays], [input_peers]) with 'count' items each"""

        r = self.invoke(GetDialogsRequest(offset_date=offset_date,
                                          offset_id=offset_id,
                                          offset_peer=offset_peer,
                                          limit=count))

        return (r.dialogs,
                [self.find_display_name(d.peer, r.users, r.chats) for d in r.dialogs],
                [self.find_input_peer(d.peer, r.users, r.chats) for d in r.dialogs])

    # endregion

    # region Message requests

    def send_message(self, input_peer, message, markdown=False, no_web_page=False):
        """Sends a message to the given input peer"""
        if markdown:
            msg, entities = parse_message_entities(message)
        else:
            msg, entities = message, []

        self.invoke(SendMessageRequest(peer=input_peer,
                                       message=msg,
                                       random_id=utils.generate_random_long(),
                                       entities=entities,
                                       no_webpage=no_web_page))

    def get_message_history(self, input_peer, limit=20,
                            offset_date=None, offset_id=0, max_id=0, min_id=0, add_offset=0):
        """
        Gets the message history for the specified InputPeer

        :param input_peer:  The InputPeer from whom to retrieve the message history
        :param limit:       Number of messages to be retrieved
        :param offset_date: Offset date (messages *previous* to this date will be retrieved)
        :param offset_id:   Offset message ID (only messages *previous* to the given ID will be retrieved)
        :param max_id:      All the messages with a higher (newer) ID or equal to this will be excluded
        :param min_id:      All the messages with a lower (older) ID or equal to this will be excluded
        :param add_offset:  Additional message offset (all of the specified offsets + this offset = older messages)

        :return: A tuple containing total message count and two more lists ([messages], [senders]).
                 Note that the sender can be null if it was not found!
        """
        result = self.invoke(GetHistoryRequest(input_peer,
                                               limit=limit,
                                               offset_date=offset_date,
                                               offset_id=offset_id,
                                               max_id=max_id,
                                               min_id=min_id,
                                               add_offset=add_offset))

        # The result may be a messages slice (not all messages were retrieved) or
        # simply a messages TLObject. In the later case, no "count" attribute is specified:
        # the total messages count is retrieved by counting all the retrieved messages
        total_messages = getattr(result, 'count', len(result.messages))

        # Iterate over all the messages and find the sender User
        users = []
        for msg in result.messages:
            for usr in result.users:
                if msg.from_id == usr.id:
                    users.append(usr)
                    break

        return total_messages, result.messages, users

    # endregion

    # TODO Handle media downloading/uploading in a different session?
    #  "It is recommended that large queries (upload.getFile, upload.saveFilePart)
    #   be handled through a separate session and a separate connection"
    # region Uploading media requests

    def upload_file(self, file_path, part_size_kb=64, file_name=None):
        """Uploads the specified media with the given chunk (part) size, in KB.
           If no custom file name is specified, the real file name will be used.
           This method will fail when trying to upload files larger than 10MB!"""

        part_size = int(part_size_kb * 1024)
        if part_size % 1024 != 0:
            raise ValueError('The part size must be evenly divisible by 1024')

        # Multiply the datetime timestamp by 10^6 to get the ticks
        # This is high likely going to be unique
        file_id = int(datetime.now().timestamp() * (10 ** 6))
        part_index = 0
        hash_md5 = md5()

        with open(file_path, 'rb') as file:
            while True:
                # Read the file by in chunks of size part_size
                part = file.read(part_size)

                # If we have read no data (0 bytes), the file is over
                # So there is nothing left to upload
                if not part:
                    break

                print('I read {} out of {}'.format(len(part), part_size))

                # Invoke the file upload and increment both the part index and MD5 checksum
                result = self.invoke(SaveFilePartRequest(file_id, part_index, part))
                if result:
                    part_index += 1
                    hash_md5.update(part)
                else:
                    raise ValueError('Could not upload file part #{}'.format(part_index))

        # Set a default file name if None was specified
        if not file_name:
            file_name = path.basename(file_path)

        # After the file has been uploaded, we can return a handle pointing to it
        return InputFile(id=file_id,
                         parts=part_index,
                         name=file_name,
                         md5_checksum=hash_md5.hexdigest())

    def send_photo_file(self, input_file, input_peer, caption=''):
        """Sends a previously uploaded input_file
           (which should be a photo) to an input_peer"""
        self.send_media_file(
            InputMediaUploadedPhoto(input_file, caption), input_peer)

    def send_document_file(self, input_file, input_peer, caption=''):
        """Sends a previously uploaded input_file
           (which should be a document) to an input_peer"""

        # Determine mime-type and attributes
        # Take the first element by using [0] since it returns a tuple
        mime_type = guess_type(input_file.name)[0]
        attributes = [
            DocumentAttributeFilename(input_file.name)
            # TODO If the input file is an audio, find out:
            # Performer and song title and add DocumentAttributeAudio
        ]
        self.send_media_file(InputMediaUploadedDocument(file=input_file,
                                                        mime_type=mime_type,
                                                        attributes=attributes,
                                                        caption=caption), input_peer)

    def send_media_file(self, input_media, input_peer):
        """Sends any input_media (contact, document, photo...) to an input_peer"""
        self.invoke(SendMediaRequest(peer=input_peer,
                                     media=input_media,
                                     random_id=utils.generate_random_long()))

    # endregion

    # region Downloading media requests

    def download_msg_media(self, message_media, file_path, add_extension=True):
        """Downloads the given MessageMedia (Photo, Document or Contact)
           into the desired file_path, optionally finding its extension automatically"""
        if type(message_media) == MessageMediaPhoto:
            return self.download_photo(message_media, file_path, add_extension)

        elif type(message_media) == MessageMediaDocument:
            return self.download_document(message_media, file_path, add_extension)

        elif type(message_media) == MessageMediaContact:
            return self.download_contact(message_media, file_path, add_extension)

    def download_photo(self, message_media_photo, file_path, add_extension=False):
        """Downloads MessageMediaPhoto's largest size into the desired
           file_path, optionally finding its extension automatically"""
        # Determine the photo and its largest size
        photo = message_media_photo.photo
        largest_size = photo.sizes[-1].location

        # Photos are always compressed into a .jpg by Telegram
        if add_extension:
            file_path += '.jpg'

        # Download the media with the largest size input file location
        self.download_file_loc(InputFileLocation(volume_id=largest_size.volume_id,
                                                 local_id=largest_size.local_id,
                                                 secret=largest_size.secret), file_path)
        return file_path

    def download_document(self, message_media_document, file_path=None, add_extension=True):
        """Downloads the given MessageMediaDocument into the desired
           file_path, optionally finding its extension automatically.
           If no file_path is given, it will _try_ to be guessed from the document"""
        document = message_media_document.document

        # If no file path was given, try to guess it from the attributes
        if file_path is None:
            for attr in document.attributes:
                if type(attr) == DocumentAttributeFilename:
                    file_path = attr.file_name
                    break  # This attribute has higher preference

                elif type(attr) == DocumentAttributeAudio:
                    file_path = '{} - {}'.format(attr.performer, attr.title)

            if file_path is None:
                print('Could not determine a filename for the document')

        # Guess the extension based on the mime_type
        if add_extension:
            ext = guess_extension(document.mime_type)
            if ext is not None:
                file_path += ext

        self.download_file_loc(InputDocumentFileLocation(id=document.id,
                                                         access_hash=document.access_hash,
                                                         version=document.version), file_path)

        return file_path

    @staticmethod
    def download_contact(message_media_contact, file_path, add_extension=True):
        """Downloads a media contact using the vCard 4.0 format"""

        first_name = message_media_contact.first_name
        last_name = message_media_contact.last_name
        phone_number = message_media_contact.phone_number

        # The only way we can save a contact in an understandable
        # way by phones is by using the .vCard format
        if add_extension:
            file_path += '.vcard'

        # Ensure that we'll be able to download the contact
        utils.ensure_parent_dir_exists(file_path)

        with open(file_path, 'w', encoding='utf-8') as file:
            file.write('BEGIN:VCARD\n')
            file.write('VERSION:4.0\n')
            file.write('N:{};{};;;\n'.format(first_name, last_name if last_name else ''))
            file.write('FN:{}\n'.format(' '.join((first_name, last_name))))
            file.write('TEL;TYPE=cell;VALUE=uri:tel:+{}\n'.format(phone_number))
            file.write('END:VCARD\n')

        return file_path

    def download_file_loc(self, input_location, file_path, part_size_kb=64):
        """Downloads media from the given input_file_location to the specified file_path"""

        part_size = int(part_size_kb * 1024)
        if part_size % 1024 != 0:
            raise ValueError('The part size must be evenly divisible by 1024')

        # Ensure that we'll be able to download the media
        utils.ensure_parent_dir_exists(file_path)

        # Start with an offset index of 0
        offset_index = 0
        with open(file_path, 'wb') as file:
            while True:
                # The current offset equals the offset_index multiplied by the part size
                offset = offset_index * part_size
                result = self.invoke(GetFileRequest(input_location, offset, part_size))
                offset_index += 1

                # If we have received no data (0 bytes), the file is over
                # So there is nothing left to download and write
                if not result.bytes:
                    return result.type  # Return some extra information

                file.write(result.bytes)

    # endregion

    # endregion

    # region Utilities

    @staticmethod
    def find_display_name(peer, users, chats):
        """Searches the display name for peer in both users and chats.
           Returns None if it was not found"""
        try:
            if type(peer) is PeerUser:
                user = next(u for u in users if u.id == peer.user_id)
                if user.last_name is not None:
                    return '{} {}'.format(user.first_name, user.last_name)
                return user.first_name

            elif type(peer) is PeerChat:
                return next(c for c in chats if c.id == peer.chat_id).title

            elif type(peer) is PeerChannel:
                return next(c for c in chats if c.id == peer.channel_id).title

        except StopIteration:
            pass

        return None

    @staticmethod
    def find_input_peer(peer, users, chats):
        """Searches the given peer in both users and chats and returns an InputPeer for it.
           Returns None if it was not found"""
        try:
            if type(peer) is PeerUser:
                user = next(u for u in users if u.id == peer.user_id)
                return InputPeerUser(user.id, user.access_hash)

            elif type(peer) is PeerChat:
                chat = next(c for c in chats if c.id == peer.chat_id)
                return InputPeerChat(chat.id)

            elif type(peer) is PeerChannel:
                channel = next(c for c in chats if c.id == peer.channel_id)
                return InputPeerChannel(channel.id, channel.access_hash)

        except StopIteration:
            return None

    # endregion

    # region Updates handling

    def add_update_handler(self, handler):
        """Adds an update handler (a function which takes a TLObject,
          an update, as its parameter) and listens for updates"""
        self.sender.add_update_handler(handler)

    def remove_update_handler(self, handler):
        self.sender.remove_update_handler(handler)

    # endregion
