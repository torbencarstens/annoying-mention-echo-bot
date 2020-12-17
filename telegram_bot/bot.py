import json
import tempfile
from datetime import datetime, timedelta
from enum import Enum
from itertools import zip_longest
from threading import Timer
from typing import Any, List, Optional, Dict, Iterable, Tuple

from telegram import ParseMode, TelegramError, Update, Message, ChatPermissions
from telegram.error import BadRequest
from telegram.ext import CallbackContext, Updater

from .chat import Chat, User
from .decorators import Command
from .logger import create_logger


def grouper(iterable, n, fillvalue=None) -> Iterable[Tuple[Any, Any]]:
    """Collect data into fixed-length chunks or blocks"""
    args = [iter(iterable)] * n
    return zip_longest(*args, fillvalue=fillvalue)


class SpamType(Enum):
    NONE = 0
    CONSECUTIVE = 1
    DIFFERENT = 2
    SAME = 3


class Bot:
    def __init__(self, updater: Updater):
        self.chats: Dict[str, Chat] = {}
        self.updater = updater
        self.state: Dict[str, Any] = {
            "main_id": None
        }
        self.logger = create_logger("regular_dicers_bot")

    def save_state(self) -> None:
        self.state["chats"] = [chat.serialize() for chat in self.chats.values()]
        with open("state.json", "w+") as f:
            json.dump(self.state, f)

    @Command(chat_admin=True)
    def delete_chat(self, update: Update, context: CallbackContext) -> None:
        chat: Chat = context.chat_data["chat"]

        if chat.id in self.chats:
            self.logger.info(f"Deleting chat ({chat}) from state.")
            del self.chats[chat.id]
            del context.chat_data["chat"]

    def set_user_restriction(self, chat_id: str, user: User, until_date: timedelta, permissions: ChatPermissions,
                             reason: str = None) -> bool:
        timestamp: int = int((datetime.now() + until_date).timestamp())
        try:
            result: bool = self.updater.bot.restrict_chat_member(chat_id, user.id, permissions, until_date=timestamp)
            if not permissions.can_send_messages:
                datestring: str = str(until_date).rsplit(".")[0]  # str(timedelta) -> [D day[s], ][H]H:MM:SS[.UUUUUU]
                message = f"{user.name} has been restricted for {datestring}."
                if reason:
                    message += f"\nReason: {reason}"
                self.updater.bot.send_message(chat_id=chat_id,
                                              text=message, disable_notification=True)
        except TelegramError as e:
            if e.message == "Can't demote chat creator" and not permissions.can_send_messages:
                message = "Sadly, user {} couldn't be restricted due to: `{}`. Shame on {}".format(user.name,
                                                                                                   e.message,
                                                                                                   user.name)
                self.logger.debug("{}".format(message))
                self.updater.bot.send_message(chat_id=chat_id, text=message, parse_mode=ParseMode.MARKDOWN)
            self.logger.error(e)
            result = False

        return result

    def unmute_user(self, chat_id: str, user: User) -> bool:
        result = False
        permissions = ChatPermissions(can_send_messages=True, can_send_media_messages=True,
                                      can_send_other_messages=True, can_add_web_page_previews=True)

        try:
            # if self.updater.bot.promote_chat_member(chat_id, user.id, can_post_messages=True):
            if self.set_user_restriction(chat_id, user, timedelta(minutes=0), permissions):
                user.muted = False
                result = True
            else:
                self.logger.error("Failed to unmute user")
        except TelegramError:
            self.logger.error("Error while promoting chat member", exc_info=True)

        return result

    def mute_user(self, chat_id: str, user: User, until_date: timedelta, reason: Optional[str] = None) -> bool:
        if user.muted:
            return True

        permissions = ChatPermissions(can_send_messages=False)
        result = False
        self.logger.info(f"Reason for muting: {reason}")
        if self.set_user_restriction(chat_id, user, until_date=until_date, reason=reason, permissions=permissions):
            user.muted = True
            result = True

            # We'd need to parse the exception before assigning user.muted differently
            def _set_user_unmute():
                user.muted = False

            self.logger.info(f"Set timer for {until_date.total_seconds()}s to set user mute state to `False`")
            Timer(until_date.total_seconds(), _set_user_unmute).start()

        return result

    @Command()
    def handle_message(self, update: Update, context: CallbackContext) -> None:
        self.logger.info("Handle message: {}".format(update.effective_message.text))
        message_user: User = context.user_data["user"]
        chat: Chat = context.chat_data["chat"]

        message = ", ".join([user.markdown_mention() for user in chat.users_to_mention() if user.name != message_user.name])

        update.message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

    @Command()
    def annoy_users_list(self, update: Update, context: CallbackContext) -> None:
        chat: Chat = context.chat_data["chat"]
        usernames = context.args

        for user in chat.users:
            if user.name in usernames:
                user.annoy = True
            else:
                user.annoy = False

    @Command()
    def handle_left_chat_member(self, update: Update, context: CallbackContext) -> None:
        chat: Chat = context.chat_data["chat"]

        if update.effective_message.left_chat_member.id != self.updater.bot.id:
            try:
                user: User = [user for user in chat.users if user.id == update.effective_message.left_chat_member.id][0]
            except IndexError:
                self.logger.error("Couldn't find user in chat")
            else:
                chat.users.remove(user)
                update.effective_message.reply_text("Bye bye birdie")

    def set_state(self, state: Dict[str, Any]) -> None:
        self.state = state
        self.chats = {schat["id"]: Chat.deserialize(schat, self.updater.bot) for schat in state.get("chats", [])}

    def send_message(self, *args, **kwargs) -> Message:
        return self.updater.bot.send_message(*args, **kwargs)

    @Command()
    def show_users(self, update: Update, context: CallbackContext) -> Optional[Message]:
        chat: Chat = context.chat_data["chat"]

        sorted_users: List[User] = sorted(chat.users, key=lambda _user: _user.name)

        if sorted_users:
            message = "\n".join([user.name for user in sorted_users])
        else:
            message = "No active users. Users need to write a message in the chat to be recognized (not just a command)"

        return self.send_message(chat_id=chat.id, text=message)

    @Command()
    def new_member(self, update: Update, context: CallbackContext) -> None:
        chat = context.chat_data["chat"]

        self.logger.info(f"New member(s) have joined this chat")

        for member in update.effective_message.new_chat_members:
            if member.id != self.updater.bot.id:
                chat.users.add(User.from_tuser(member))
                message = f"Welcome, fellow 'human' [{member.first_name}](tg://user?id={member.id})"
                update.effective_message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

    @Command()
    def status(self, update: Update, context: CallbackContext) -> Message:
        return update.effective_message.reply_text(text=f"{context.chat_data['chat']}")

    @Command()
    def version(self, update: Update, context: CallbackContext) -> Message:
        return update.effective_message.reply_text("{{VERSION}}")

    @Command()
    def server_time(self, update: Update, context: CallbackContext) -> Message:
        return update.effective_message.reply_text(datetime.now().strftime("%d-%m-%Y %H-%M-%S"))

    @Command()
    def get_data(self, update: Update, context: CallbackContext) -> Message:
        chat: Chat = context.chat_data["chat"]
        data = [_chat for _chat in self.state.get("chats", []) if _chat.get("id") == chat.id]

        if data:
            with tempfile.TemporaryFile() as temp:
                temp.write(json.dumps(data[0]).encode("utf-8"))
                temp.seek(0)
                return self.updater.bot.send_document(chat_id=chat.id, document=temp, filename=f"{chat.title}.json")
        else:
            return update.effective_message.reply_text("Couldn't find any data for this chat.")

    @Command(chat_admin=True)
    def mute(self, update: Update, context: CallbackContext):
        if not context.args:
            message = "Please provide a user and an optional timeout (`/mute <user> [<timeout in minutes>] [<reason>]`)"
            self.logger.warning("No arguments have been provided, don't execute `mute`.")
            return self.updater.bot.send_message(chat_id=update.message.chat_id, text=message,
                                                 parse_mode=ParseMode.MARKDOWN)

        username = context.args[0]
        minutes = 15
        reason = " ".join(context.args[2:])

        try:
            minutes = int(context.args[1])
        except (IndexError, ValueError):
            self.logger.error("Exception while getting time string from mute command", exc_info=True)

        mute_time = timedelta(minutes=minutes)
        chat = context.chat_data["chat"]

        try:
            user = next(filter(lambda x: x.name == username, chat.users))
        except StopIteration:
            self.logger.warning(f"Couldn't find user {username} in users for chat {update.message.chat_id}",
                                exc_info=True)
            update.effective_message.reply_text(f"Can't mute {username} (not found in current chat).")
        else:
            self.mute_user(update.message.chat_id, user, until_date=mute_time, reason=reason)

    @Command(chat_admin=True)
    def unmute(self, update: Update, context: CallbackContext):
        if not context.args:
            message = "You have to provide a user which should be unmuted."
            self.logger.warning("No arguments have been provided, don't execute `unmute`.")
            return update.effective_message.reply_text(message, parse_mode=ParseMode.MARKDOWN)

        username: str = context.args[0].strip()
        chat: Chat = context.chat_data["chat"]

        # @all is an unusable username
        if username == "@all":
            for user in chat.users:
                try:
                    self.unmute_user(chat.id, user)
                except BadRequest:
                    self.logger.error(f"Failed to unmute user ({user})")

            return

        try:
            user = next(filter(lambda x: x.name.lower() == username.lower(), chat.users))
        except StopIteration:
            self.logger.warning(f"Couldn't find user {username} in users for chat {update.message.chat_id}",
                                exc_info=True)
            update.effective_message.reply_text(f"Can't unmute {username} (not found in current chat).")
        else:
            if self.unmute_user(chat.id, user):
                update.effective_message.reply_text(f"Successfully unmuted {username}.")
            else:
                update.effective_message.reply_text(f"Failed to unmute {username}.")

    @Command()
    def handle_unknown_command(self, update: Update, context: CallbackContext):
        user: User = context.user_data["user"]
        chat: Chat = context.chat_data["chat"]

        reason = "This is not a valid command fuckwit."
        self.mute_user(chat_id=chat.id, user=user, until_date=timedelta(minutes=15), reason=reason)

    def kick_user(self, chat: Chat, user: User):
        return self.updater.bot.kick_chat_member(chat_id=chat.id, user_id=user.id)

    @Command(chat_admin=True)
    def kick(self, update: Update, context: CallbackContext):
        chat: Chat = context.chat_data["chat"]

        if not context.args:
            message = "Please provide a user and an optional reason(`/kick <user> [<reason>]`)"
            self.logger.warning("No arguments have been provided, don't execute `kick`.")
            return update.message.reply_text(text=message, parse_mode=ParseMode.MARKDOWN)

        username = context.args[0]
        reason = " ".join(context.args[1:])

        try:
            user: User = next(filter(lambda x: x.name == username, chat.users))
        except StopIteration:
            self.logger.warning(f"Couldn't find user {username} in users for chat {update.message.chat_id}",
                                exc_info=True)
            update.effective_message.reply_text(f"Can't kick {username} (not found in current chat).")
        else:
            try:
                result = self.kick_user(chat, user)
            except TelegramError as e:
                message = f"Couldn't remove {user.name} from chat due to error ({e})"
                self.logger.error(message)
                update.message.reply_text(message)
            else:
                if result:
                    message = f"{user.name} was kicked from chat"
                    message += f" due to {reason}." if reason else "."
                    self.logger.debug(message)
                    chat.users.remove(user)
                    update.message.reply_text(message)
                else:
                    message = f"{user.name} couldn't be kicked from chat"
                    self.logger.warning(message)
                    update.effective_message.reply_text(message)


def _split_messages(lines):
    message_length = 1024
    messages = []
    current_length = 0
    current_message = 0
    for line in lines:
        if len(messages) <= current_message:
            messages.append([])

        line_length = len(line)
        if current_length + line_length < message_length:
            current_length += line_length
            messages[current_message].append(line)
        else:
            current_length = 0
            current_message += 1

    return messages
