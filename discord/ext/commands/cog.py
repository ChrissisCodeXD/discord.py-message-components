# -*- coding: utf-8 -*-

"""
The MIT License (MIT)

Copyright (c) 2015-present Rapptz

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.
"""

import inspect
import copy
import re
import typing
from typing import Optional, Union, List, Callable, Awaitable, Pattern, AnyStr, Any
from ._types import _BaseCommand

__all__ = (
    'CogMeta',
    'Cog',
)

from ... import InvalidArgument

from ...application_commands import generate_options, SlashCommand, SubCommandGroup, SubCommand, GuildOnlySubCommand, \
    GuildOnlySlashCommand, MessageCommand, UserCommand, GuildOnlySubCommandGroup


class CogMeta(type):
    """A metaclass for defining a cog.

    Note that you should probably not use this directly. It is exposed
    purely for documentation purposes along with making custom metaclasses to intermix
    with other metaclasses such as the :class:`abc.ABCMeta` metaclass.

    For example, to create an abstract cog mixin class, the following would be done.

    .. code-block:: python3

        import abc

        class CogABCMeta(sub_commands.CogMeta, abc.ABCMeta):
            pass

        class SomeMixin(metaclass=abc.ABCMeta):
            pass

        class SomeCogMixin(SomeMixin, sub_commands.Cog, metaclass=CogABCMeta):
            pass

    .. note::

        When passing an attribute of a metaclass that is documented below, note
        that you must pass it as a keyword-only argument to the class creation
        like the following example:

        .. code-block:: python3

            class MyCog(sub_commands.Cog, name='My Cog'):
                pass

    Attributes
    -----------
    name: :class:`str`
        The cog name. By default, it is the name of the class with no modification.
    description: :class:`str`
        The cog description. By default, it is the cleaned docstring of the class.

        .. versionadded:: 1.6

    command_attrs: :class:`dict`
        A list of attributes to apply to every command inside this cog. The dictionary
        is passed into the :class:`Command` options at ``__init__``.
        If you specify attributes inside the command attribute in the class, it will
        override the one specified inside this attribute. For example:

        .. code-block:: python3

            class MyCog(sub_commands.Cog, command_attrs=dict(hidden=True)):
                @sub_commands.command()
                async def foo(self, ctx):
                    pass # hidden -> True

                @sub_commands.command(hidden=False)
                async def bar(self, ctx):
                    pass # hidden -> False
    """
    __application_commands_by_type__ = {'chat_input': {}, 'message': {}, 'user': {}}
    __guild_specific_application_commands__ = {}

    def __new__(cls, *args, **kwargs):
        name, bases, attrs = args
        attrs['__cog_name__'] = kwargs.pop('name', name)
        attrs['__cog_settings__'] = kwargs.pop('command_attrs', {})

        description = kwargs.pop('description', None)
        if description is None:
            description = inspect.cleandoc(attrs.get('__doc__', ''))
        attrs['__cog_description__'] = description

        commands = {}
        listeners = {}
        cog_interaction_listeners = {}

        no_bot_cog = 'Commands or listeners must not start with cog_ or bot_ (in method {0.__name__}.{1})'

        new_cls = super().__new__(cls, name, bases, attrs, **kwargs)
        for base in reversed(new_cls.__mro__):
            for elem, value in base.__dict__.items():
                if elem in commands:
                    del commands[elem]
                if elem in listeners:
                    del listeners[elem]

                is_static_method = isinstance(value, staticmethod)
                if is_static_method:
                    value = value.__func__
                if isinstance(value, _BaseCommand):
                    if is_static_method:
                        raise TypeError('Command in method {0}.{1!r} must not be staticmethod.'.format(base, elem))
                    if elem.startswith(('cog_', 'bot_')):
                        raise TypeError(no_bot_cog.format(base, elem))
                    commands[elem] = value

                elif inspect.iscoroutinefunction(value):
                    try:
                        getattr(value, '__cog_listener__')
                    except AttributeError:
                        try:
                            getattr(value, '__cog_interaction_listener__')
                        except AttributeError:
                            continue
                        else:
                            if elem.startswith(('cog_', 'bot_')):
                                raise TypeError(no_bot_cog.format(base, elem))
                            cog_interaction_listeners[elem] = value
                    else:
                        if elem.startswith(('cog_', 'bot_')):
                            raise TypeError(no_bot_cog.format(base, elem))
                        listeners[elem] = value

        new_cls.__cog_commands__ = list(commands.values()) # this will be copied in Cog.__new__

        listeners_as_list = []
        for listener in listeners.values():
            for listener_name in listener.__cog_listener_names__:
                # I use __name__ instead of just storing the value so I can inject
                # the self attribute when the time comes to add them to the bot
                listeners_as_list.append((listener_name, listener.__name__))

        interaction_listeners_as_list = []
        for interaction_listener in cog_interaction_listeners.values():
            for listener_name, custom_id in interaction_listener.__interaction_listener_names__:
                interaction_listeners_as_list.append((listener_name, interaction_listener.__name__, custom_id))

        new_cls.__cog_listeners__ = listeners_as_list
        new_cls.__cog_interaction_listeners__ = interaction_listeners_as_list

        return new_cls

    def __init__(self, *args, **kwargs):
        super().__init__(*args)

    @classmethod
    def qualified_name(cls):
        return cls.__cog_name__


def _cog_special_method(func):
    func.__cog_special_method__ = None
    return func


class Cog(metaclass=CogMeta):
    """The base class that all cogs must inherit from.

    A cog is a collection of sub_commands, listeners, and optional state to
    help group sub_commands together. More information on them can be found on
    the :ref:`ext_commands_cogs` page.

    When inheriting from this class, the options shown in :class:`CogMeta`
    are equally valid here.
    """

    def __new__(cls, *args, **kwargs):
        # For issue 426, we need to store a copy of the command objects
        # since we modify them to inject `self` to them.
        # To do this, we need to interfere with the Cog creation process.
        self = super().__new__(cls)
        cmd_attrs = cls.__cog_settings__

        # Either update the command with the cog provided defaults or copy it.
        self.__cog_commands__ = tuple(c._update_copy(cmd_attrs) for c in cls.__cog_commands__)

        # set the functions of the commands to the method itself

        self.__application_commands_by_type__ = copy.copy(cls.__application_commands_by_type__)

        self.__guild_specific_application_commands__ = copy.copy(cls.__guild_specific_application_commands__)

        # TODO: solve this thing on a better way
        cls.__class__.__application_commands_by_type__ = {'chat_input': {}, 'message': {}, 'user': {}}
        cls.__class__.__guild_specific_application_commands__ = {}

        lookup = {
            cmd.qualified_name: cmd
            for cmd in self.__cog_commands__
        }

        # Update the Command instances dynamically as well
        for command in self.__cog_commands__:
            setattr(self, command.callback.__name__, command)
            parent = command.parent
            if parent is not None:
                # Get the latest parent reference
                parent = lookup[parent.qualified_name]

                # Update our parent's reference to our self
                parent.remove_command(command.name)
                parent.add_command(command)
        return self

    def get_commands(self):
        r"""
        Returns
        --------
        List[:class:`.Command`]
            A :class:`list` of :class:`.Command`\s that are
            defined inside this cog.

            .. note::

                This does not include subcommands.
        """
        return [c for c in self.__cog_commands__ if c.parent is None]

    @property
    def qualified_name(self):
        """:class:`str`: Returns the cog's specified name, not the class name."""
        return self.__cog_name__

    @property
    def description(self):
        """:class:`str`: Returns the cog's description, typically the cleaned docstring."""
        return self.__cog_description__

    @description.setter
    def description(self, description):
        self.__cog_description__ = description

    def walk_commands(self):
        """An iterator that recursively walks through this cog's sub_commands and subcommands.

        Yields
        ------
        Union[:class:`.Command`, :class:`.Group`]
            A command or group from the cog.
        """
        from .core import GroupMixin
        for command in self.__cog_commands__:
            if command.parent is None:
                yield command
                if isinstance(command, GroupMixin):
                    yield from command.walk_commands()

    def get_listeners(self):
        """Returns a :class:`list` of (name, function) listener pairs that are defined in this cog.

        Returns
        --------
        List[Tuple[:class:`str`, :ref:`coroutine <coroutine>`]]
            The listeners defined in this cog.
        """
        return [(name, getattr(self, method_name)) for name, method_name in self.__cog_listeners__]

    @classmethod
    def get_application_commands(cls):
        return [()]

    @classmethod
    def _get_overridden_method(cls, method):
        """Return None if the method is not overridden. Otherwise returns the overridden method."""
        return getattr(method.__func__, '__cog_special_method__', method)

    @classmethod
    def listener(cls, name=None):
        """A decorator that marks a function as a listener.

        This is the cog equivalent of :meth:`.Bot.listen`.

        Parameters
        ------------
        name: :class:`str`
            The name of the event being listened to. If not provided, it
            defaults to the function's name.

        Raises
        --------
        TypeError
            The function is not a coroutine function or a string was not passed as
            the name.
        """

        if name is not None and not isinstance(name, str):
            raise TypeError('Cog.listener expected str but received {0.__class__.__name__!r} instead.'.format(name))

        def decorator(func):
            actual = func
            if isinstance(actual, staticmethod):
                actual = actual.__func__
            if not inspect.iscoroutinefunction(actual):
                raise TypeError('Listener function must be a coroutine function.')
            actual.__cog_listener__ = True
            to_assign = name or actual.__name__
            try:
                actual.__cog_listener_names__.append(to_assign)
            except AttributeError:
                actual.__cog_listener_names__ = [to_assign]
            # we have to return `func` instead of `actual` because
            # we need the type to be `staticmethod` for the metaclass
            # to pick it up but the metaclass unfurls the function and
            # thus the assignments need to be on the actual function
            return func
        return decorator
    
    @classmethod
    def on_click(cls, custom_id: Optional[Union[Pattern[AnyStr], AnyStr]] = None) -> Callable[
        [Awaitable[Any]], Awaitable[Any]
    ]:
        """
        A decorator that registers a raw_button_click event that checks on execution if the ``custom_id's`` are the same;
        if so, the :func:`func` is called.

        The function this is attached to must take the same parameters as a
        `raw_button_click-Event <https://discordpy-message-components.rtfd.io/en/latest/addition.html#on_raw_button_click>`_.

        .. important::
            The func must be a coroutine, if not, :exc:`TypeError` is raised.

        Parameters
        ----------
        custom_id: Optional[Union[Pattern[AnyStr], AnyStr]]
            If the :attr:`custom_id` of the :class:`discord.Button` could not use as an function name
            or you want to give the function a different name then the custom_id use this one to set the custom_id.
            You can also specify a regex and if the custom_id matches it, the function will be executed.

        Example
        -------
        .. code-block:: python3

            # the Button
            Button(label='Hey im a cool blue Button',
                   custom_id='cool blue Button',
                   style=ButtonStyle.blurple)

            # function that's called when the Button pressed
            @sub_commands.Cog.on_click(custom_id='cool blue Button')
            async def cool_blue_button(i: discord.Interaction, button):
                await i.respond(f'Hey you pressed a `{button.custom_id}`!', hidden=True)

        Raises
        ------
        TypeError
            The coroutine passed is not actually a coroutine.
        """
        def decorator(func: Awaitable[Any]) -> Awaitable[Any]:
            actual = func
            if isinstance(actual, staticmethod):
                actual = actual.__func__
            if not inspect.iscoroutinefunction(actual):
                raise TypeError('event registered must be a coroutine function')
            actual.__cog_interaction_listener__ = True
            _custom_id = re.compile(custom_id) if (
                    custom_id is not None and not isinstance(custom_id, re.Pattern)
            ) else re.compile(actual.__name__)
            try:
                actual.__interaction_listener_names__.append(('raw_button_click', _custom_id))
            except AttributeError:
                actual.__interaction_listener_names__ = [('raw_button_click', _custom_id)]
            return func
        return decorator

    @classmethod
    def on_select(cls, custom_id: Optional[Union[Pattern[AnyStr], AnyStr]] = None) -> Callable[
        [Awaitable[Any]], Awaitable[Any]
    ]:
        """
        A decorator with which you can assign a function to a specific :class:`SelectMenu` (or its custom_id).

        The function this is attached to must take the same parameters as a
        `raw_selection_select-Event <https://discordpy-message-components.rtfd.io/en/latest/addition.html#on_raw_selection_select>`_.

        .. important::
            The func must be a coroutine, if not, :exc:`TypeError` is raised.

        Parameters
        -----------
        custom_id: Optional[Union[Pattern[AnyStr], AnyStr]]
            If the :attr:`custom_id` of the :class:`discord.SelectMenu` could not use as an function name
            or you want to give the function a different name then the custom_id use this one to set the custom_id.
            You can also specify a regex and if the custom_id matches it, the function will be executed.

        Example
        -------

        .. code-block:: python

            # the SelectMenu
            SelectMenu(custom_id='choose_your_gender',
                    options=[
                            select_option(label='Female', value='Female', emoji='♀️'),
                            select_option(label='Male', value='Male', emoji='♂️'),
                            select_option(label='Trans/Non Binary', value='Trans/Non Binary', emoji='⚧')
                            ], placeholder='Choose your Gender')

            # function that's called when the SelectMenu is used
            @sub_commands.Cog.on_select()
            async def choose_your_gender(i: discord.Interaction, select_menu):
                await i.respond(f'You selected `{select_menu.values[0]}`!', hidden=True)

        Raises
        --------
        TypeError
            The coroutine passed is not actually a coroutine.
        """
        def decorator(func: Awaitable[Any]) -> Awaitable[Any]:
            actual = func
            if isinstance(actual, staticmethod):
                actual = actual.__func__
            if not inspect.iscoroutinefunction(actual):
                raise TypeError('event registered must be a coroutine function')
            actual.__cog_interaction_listener__ = True
            _custom_id = re.compile(custom_id) if (
                    custom_id is not None and not isinstance(custom_id, re.Pattern)
            ) else re.compile(actual.__name__)
            try:
                actual.__interaction_listener_names__.append(('raw_selection_select', _custom_id))
            except AttributeError:
                actual.__interaction_listener_names__ = [('raw_selection_select', _custom_id)]
            return func
        return decorator

    @classmethod
    def slash_command(cls,
                      name: str = None,
                      description: str = None,
                      default_permission: bool = True,
                      options: list = [],
                      guild_ids: List[int] = None,
                      connector: dict = {},
                      option_descriptions: dict = {},
                      base_name: str = None,
                      base_desc: str = None,
                      group_name: str = None,
                      group_desc: str = None) -> Callable[
        [Awaitable[Any]], Union[SlashCommand, GuildOnlySlashCommand, SubCommand, GuildOnlySubCommand]
    ]:
        """
        A decorator that adds a slash-command to the client.

        .. note::

            :attr:`sync_commands` of the :class:`Client`-instance or the class, that inherits from it
            must be set to ``True`` to register a command if he not already exist and update him if changes where made.

        :param name:
            The name of the command. Must only contain a-z, _ and - and be 1-32 characters long.
            Default to the functions name.
        :type name: Optional[:class:`str`]
        :param description:
            The description of the command shows up in the client. Must be between 1-100 characters long.
            Default to the functions docstring or "No Description".
        :type description: Optional[:class:`str`]
        :param default_permission: Optional[:class:`bool`]
            Whether the command should be usable by any user by default, default ``True``.
            If set to ``False`` the command will not be available in Direct Messages.
        :type default_permission: Optional[:class:`bool`]
        :param options:
            A list of max. 25 options for the command. If not provided the options will be generated
            using :meth:`generate_options` that creates the options out of the function parameters.
            Required options **must** be listed before optional ones.
            Use :param:`options` to connect non-ascii option names with the parameter of the function.
        :type options: Optional[List[:class:`SlashCommandOption`]]
        :param guild_ids:
            ID's of guilds this command should be registered in. If empty, the command will be global.
        :type guild_ids: Optional[List[:class:`int`]]
        :param connector:
            A dictionary containing the name of function-parameters as keys and the name of the option as values.
            Useful for using non-ascii Letters in your option names without getting ide-errors.
        :type connector: Optional[Dict[:class:`str`, :class:`str`]]
        :param option_descriptions:
            Descriptions the :func:`generate_options` should take for the Options that will be generated.
            The keys are the name of the option and the value the description.
        :type option_descriptions: Optional[Dict[:class:`str`, :class:`str`]]
        :param base_name:
            The name of the base-command(a-z, _ and -, 1-32 characters) if you want the command
            to be in a command-/sub-command-group.
            If the base-command not exists yet, he will be addet.
        :type base_name: Optional[:class:`str`]
        :param base_desc:
            The description of the base-command(1-100 characters), only needed if the :param:`base_name` was not used before
            otherwise it will replace the one before.
        :type base_desc: Optional[:class:`str`]
        :param group_name:
            The name of the command-group(a-z, _ and -, 1-32 characters) if you want the command
            to be in a sub-command-group.
        :type group_name: Optional[:class:`str`]
        :param group_desc:
            The description of the sub-command-group(1-100 characters), only needed if the :param:`group_name` was not used before
            otherwise it will replace the one before.
        :type group_desc: Optional[:class:`str`]

        :raise TypeError:
            The function the decorator is attached to is not actual a coroutine (startswith ``async def``)
            or a parameter passed to :class:`SlashCommandOption` is invalid for the option_type or the option_type
            itself is invalid.
        :raise InvalidArgument:
            You passed :param:`group_name` but no :param:`base_name`.
        :raise ValueError:
            Any of :param:`name`, :param:`description`, :param:`options`, :param:`base_name`, :param:`base_desc`, :param:`group_name` or :param:`group_desc` is not valid.

        Returns
        -------
        Callable:
            The function that wich registers the func given as a slash-command to the client and returns the generated command.
        """

        def decorator(func: Awaitable[Any]) -> Union[
            SlashCommand, GuildOnlySlashCommand, SubCommand, GuildOnlySubCommand
        ]:
            """

            Parameters
            ----------
            func:
                The function for the decorator.

            Returns
            -------
            Union[:class:`SlashCommand`, :class:`GuildOnlySlashCommand`, :class:`SubCommand`, :class:`GuildOnlySubCommand`]:
                The slash-command registered.
                If neither :param:`guild_ids` or :param:`base_name` passed: An object of :class:`SlashCommand`.
                If :param:`guild_ids` and no :param:`base_name` where passed: An object of :class:`GuildOnlySlashCommand`
                representing the guild-only slash-commands.
                If :param:`base_name` and no :param:`guild_ids` where passed: An object of class:`SubCommand`.
                if :param:`base_name` and :param:`guild_ids` passed: An object of :class:`GuildOnlySubCommand`
                representing the guild-only sub-commands.
            """
            actual = func
            if isinstance(actual, staticmethod):
                actual = actual.__func__
            if not inspect.iscoroutinefunction(actual):
                raise TypeError('The slash-command function registered  must be a coroutine.')
            _name = (name or actual.__name__).lower()
            _description = description or ((inspect.cleandoc(actual.__doc__)[:100]) if actual.__doc__ else 'No Description')
            _options = options or generate_options(actual, descriptions=option_descriptions, connector=connector, is_cog=True)
            if group_name and not base_name:
                raise InvalidArgument('You have to provide the `base_name` parameter if you want to create a SubCommand or SubCommandGroup.')
            guild_cmds = []
            if guild_ids:
                for guild_id in guild_ids:
                    base, base_command, sub_command_group = None, None, None
                    try:
                        cls.__guild_specific_application_commands__[guild_id]
                    except KeyError:
                        cls.__guild_specific_application_commands__[guild_id] = {'chat_input': {}, 'message': {}, 'user': {}}
                    if base_name:
                        try:
                            base_command = cls.__guild_specific_application_commands__[guild_id]['chat_input'][base_name]
                        except KeyError:
                            base_command = cls.__guild_specific_application_commands__[guild_id]['chat_input'][base_name] =\
                                SlashCommand(cog=cls,
                                             name=base_name,
                                             description=base_desc or 'No Description',
                                             default_permission=default_permission,
                                             guild_id=guild_id)
                        else:
                            base_command.description = base_desc or base_command.description
                            base_command.default_permission = default_permission
                        base = base_command
                    if group_name:
                        try:
                            sub_command_group = cls.__guild_specific_application_commands__[guild_id]['chat_input'][base_name]._sub_commands[group_name]
                        except KeyError:
                            sub_command_group = cls.__guild_specific_application_commands__[guild_id]['chat_input'][
                                base_name]._sub_commands[group_name] = SubCommandGroup(cog=cls,
                                                                                       parent=base_command,
                                                                                       name=group_name,
                                                                                       description=group_desc or 'No Description',
                                                                                       guild_id=guild_id)
                        else:
                            sub_command_group.description = group_desc or sub_command_group.description
                        base = sub_command_group
                    if base:
                        base._sub_commands[_name] = SubCommand(cog=cls,
                                                               parent=base,
                                                               name=_name,
                                                               description=_description,
                                                               options=_options,
                                                               connector=connector,
                                                               func=actual)
                        guild_cmds.append(base._sub_commands[_name])
                    else:
                        cls.__guild_specific_application_commands__[guild_id]['chat_input'][_name] =\
                            SlashCommand(cog=cls,
                                         name=_name,
                                         description=_description,
                                         default_permission=default_permission,
                                         options=_options,
                                         func=actual,
                                         guild_id=guild_id,
                                         connector=connector)
                        guild_cmds.append(cls.__guild_specific_application_commands__[guild_id]['chat_input'][_name])

                if base_name:
                    base = GuildOnlySlashCommand(cog=cls, name=_name, description=_description,
                                                 default_permission=default_permission, options=_options,
                                                 guild_ids=guild_ids, connector=connector,
                                                 commands=guild_cmds)
                    if group_name:
                        base = GuildOnlySubCommandGroup(cog=cls, parent=base, name=_name,
                                                        description=_description, default_permission=default_permission,
                                                        options=_options, guild_ids=guild_ids, connector=connector)
                    return GuildOnlySubCommand(cog=cls, parent=base, name=_name, description=_description,
                                               options=_options, func=actual, guild_ids=guild_ids, connector=connector,
                                               commands=guild_cmds)
                return GuildOnlySlashCommand(cog=cls, name=_name, description=_description,
                                             default_permission=default_permission, options=_options,
                                             func=actual, guild_ids=guild_ids, connector=connector)
            else:
                base, base_command, sub_command_group = None, None, None
                if base_name:
                    try:
                        base_command = cls.__application_commands_by_type__['chat_input'][base_name]
                    except KeyError:
                        base_command = cls.__application_commands_by_type__['chat_input'][base_name] = SlashCommand(
                            cog=cls,
                            name=base_name,
                            description=base_desc or 'No Description',
                            default_permission=default_permission,
                            func=actual)
                    else:
                        base_command.description = base_desc or base_command.description
                        base_command.default_permission = default_permission
                    base = base_command
                if group_name:
                    try:
                        sub_command_group = cls.__application_commands_by_type__['chat_input'][base_name]._sub_commands[group_name]
                    except KeyError:
                        sub_command_group = cls.__application_commands_by_type__['chat_input'][base_name]._sub_commands[
                            group_name] = SubCommandGroup(cog=cls,
                                                          parent=base_command,
                                                          name=group_name,
                                                          description=group_desc or 'No Description')
                    else:
                        sub_command_group.description = group_desc or sub_command_group.description
                    base = sub_command_group
                if base:
                    command = base._sub_commands[_name] = SubCommand(cog=cls,
                                                                     parent=base,
                                                                     name=_name,
                                                                     description=_description,
                                                                     options=_options,
                                                                     func=actual, connector=connector)
                else:
                    command = cls.__application_commands_by_type__['chat_input'][_name] = SlashCommand(
                        cog=cls,
                        name=_name, description=_description,
                        default_permission=default_permission,
                        options=_options, func=actual,
                        connector=connector)
                return command
        return decorator

    @classmethod
    def message_command(cls,
                        name: str = None,
                        default_permission: bool = True,
                        guild_ids: List[int] = None) -> Callable[[Awaitable[Any]], MessageCommand]:
        """
        A decorator that registers a :class:`MessageCommand`(shows up under ``Apps`` when right-clicking on a message)
        to the client.

        .. note::

            :attr:`sync_commands` of the :class:`Client`-instance or the class, that inherits from it
            must be set to ``True`` to register a command if he not already exist and update him if changes where made.

        Parameters
        ----------
        name: Optional[:class:`str`]
            The name of the message-command, default to the functions name.
            Must be between 1-32 characters long.
        default_permission: Optional[:class:`bool`]
            Whether the command should be usable by any user by default, default ``True``.
            If set to ``False`` the command will not be available in Direct Messages.
        guild_ids: Optional[List[:class:`int`]]
            ID's of guilds this command should be registered in. If empty, the command will be global.

        Returns
        -------
        MessageCommand:
            The message-command registered.

        Raises
        ------
        TypeError:
            The function the decorator is attached to is not actual a coroutine (startswith ``async def``).
        """
        def decorator(func: Awaitable[Any]) -> MessageCommand:
            actual = func
            if isinstance(actual, staticmethod):
                actual = actual.__func__
            if not inspect.iscoroutinefunction(actual):
                raise TypeError('The message-command function registered  must be a coroutine.')
            _name = name or actual.__name__
            cmd = MessageCommand(cog=cls,
                                 name=_name,
                                 default_permission=default_permission,
                                 func=actual.__name__,
                                 guild_ids=guild_ids)
            return cmd
        return decorator

    @classmethod
    def user_command(cls,
                     name: str = None,
                     default_permission: bool = True,
                     guild_ids: List[int] = None) -> Callable[[Awaitable[Any]], UserCommand]:
        """
       A decorator that registers a :class:`UserCommand`(shows up under ``Apps`` when right-clicking on a user)
       to the client.

       .. note::

           :attr:`sync_commands` of the :class:`Client`-instance or the class, that inherits from it
           must be set to ``True`` to register a command if he not already exist and update him if changes where made.

       Parameters
       ----------
       name: Optional[:class:`str`]
           The name of the user-command, default to the functions name.
           Must be between 1-32 characters long.
       default_permission: Optional[:class:`bool`]
           Whether the command should be usable by any user by default, default ``True``.
           If set to ``False`` the command will not be available in Direct Messages.
       guild_ids: Optional[List[:class:`int`]]
           ID's of guilds this command should be registered in. If empty, the command will be global.

       Returns
       -------
       UserCommand:
           The user-command registered.

       Raises
       ------
       TypeError:
           The function the decorator is attached to is not actual a coroutine (startswith ``async def``).
       """
        def decorator(func: Awaitable[Any]) -> UserCommand:
            actual = func
            if isinstance(actual, staticmethod):
                func = actual.__func__
            if not inspect.iscoroutinefunction(actual):
                raise TypeError('The user-command function registered  must be a coroutine.')
            _name = name or actual.__name__
            cmd = UserCommand(cog=cls,
                              name=_name,
                              default_permission=default_permission,
                              func=actual.__name__,
                              guild_ids=guild_ids)
            return cmd
        return decorator

    def has_error_handler(self):
        """:class:`bool`: Checks whether the cog has an error handler.

        .. versionadded:: 1.7
        """
        return not hasattr(self.cog_command_error.__func__, '__cog_special_method__')

    @_cog_special_method
    def cog_unload(self):
        """A special method that is called when the cog gets removed.

        This function **cannot** be a coroutine. It must be a regular
        function.

        Subclasses must replace this if they want special unloading behaviour.
        """
        pass

    @_cog_special_method
    def bot_check_once(self, ctx):
        """A special method that registers as a :meth:`.Bot.check_once`
        check.

        This function **can** be a coroutine and must take a sole parameter,
        ``ctx``, to represent the :class:`.Context`.
        """
        return True

    @_cog_special_method
    def bot_check(self, ctx):
        """A special method that registers as a :meth:`.Bot.check`
        check.

        This function **can** be a coroutine and must take a sole parameter,
        ``ctx``, to represent the :class:`.Context`.
        """
        return True

    @_cog_special_method
    def cog_check(self, ctx):
        """A special method that registers as a :func:`sub_commands.check`
        for every command and subcommand in this cog.

        This function **can** be a coroutine and must take a sole parameter,
        ``ctx``, to represent the :class:`.Context`.
        """
        return True

    @_cog_special_method
    async def cog_command_error(self, ctx, error):
        """A special method that is called whenever an error
        is dispatched inside this cog.

        This is similar to :func:`.on_command_error` except only applying
        to the sub_commands inside this cog.

        This **must** be a coroutine.

        Parameters
        -----------
        ctx: :class:`.Context`
            The invocation context where the error happened.
        error: :class:`CommandError`
            The error that happened.
        """
        pass

    @_cog_special_method
    async def cog_before_invoke(self, ctx):
        """A special method that acts as a cog local pre-invoke hook.

        This is similar to :meth:`.Command.before_invoke`.

        This **must** be a coroutine.

        Parameters
        -----------
        ctx: :class:`.Context`
            The invocation context.
        """
        pass

    @_cog_special_method
    async def cog_after_invoke(self, ctx):
        """A special method that acts as a cog local post-invoke hook.

        This is similar to :meth:`.Command.after_invoke`.

        This **must** be a coroutine.

        Parameters
        -----------
        ctx: :class:`.Context`
            The invocation context.
        """
        pass

    def _inject(self, bot):
        cls = self.__class__

        # realistically, the only thing that can cause loading errors
        # is essentially just the command loading, which raises if there are
        # duplicates. When this condition is met, we want to undo all what
        # we've added so far for some form of atomic loading.
        for index, command in enumerate(self.__cog_commands__):
            command.cog = self
            if command.parent is None:
                try:
                    bot.add_command(command)
                except Exception as e:
                    # undo our additions
                    for to_undo in self.__cog_commands__[:index]:
                        if to_undo.parent is None:
                            bot.remove_command(to_undo.name)
                    raise e

        # check if we're overriding the default
        if cls.bot_check is not Cog.bot_check:
            bot.add_check(self.bot_check)

        if cls.bot_check_once is not Cog.bot_check_once:
            bot.add_check(self.bot_check_once, call_once=True)

        # while Bot.add_listener can raise if it's not a coroutine,
        # this precondition is already met by the listener decorator
        # already, thus this should never raise.
        # Outside of, memory errors and the like...
        for name, method_name in self.__cog_listeners__:
            bot.add_listener(getattr(self, method_name), name)

        for (_type, method_name, custom_id) in self.__cog_interaction_listeners__:
            bot.add_interaction_listener(_type, getattr(self, method_name), custom_id)

        bot.add_application_cmds_from_cog(self)

        return self

    def _eject(self, bot):
        cls = self.__class__

        try:
            for command in self.__cog_commands__:
                if command.parent is None:
                    bot.remove_command(command.name)

            for _, method_name in self.__cog_listeners__:
                bot.remove_listener(getattr(self, method_name))

            for (_type, method_name, custom_id) in self.__cog_interaction_listeners__:
                bot.remove_interaction_listener(_type, getattr(self, method_name), custom_id)

            bot.remove_application_cmds_from_cog(self)

            if cls.bot_check is not Cog.bot_check:
                bot.remove_check(self.bot_check)

            if cls.bot_check_once is not Cog.bot_check_once:
                bot.remove_check(self.bot_check_once, call_once=True)
        finally:
            try:
                self.cog_unload()
            except Exception:
                pass
