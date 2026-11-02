"""Validated UI configuration with cached stop data and lossless option edits."""

try:
    import probatio as vol
except ImportError:  # Home Assistant releases using voluptuous.
    import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import selector

from .const import (
    CONF_CALENDAR_DAYS,
    DEFAULT_CALENDAR_DAYS,
    DOMAIN,
    MAX_CALENDAR_DAYS,
    validate_calendar_days,
)
from .coordinator import configured_stop, get_store

OptionsFlowBase = getattr(config_entries, "OptionsFlowWithReload", config_entries.OptionsFlow)


def _schema(stops, selected=None, calendar_days=None):
    options = [
        {
            "value": sid,
            "label": f"{stop.get('stop_name', 'Nieznany')} {stop.get('stop_code', '').split('/')[-1]}",
        }
        for sid, stop in stops.items()
    ]
    options.sort(key=lambda item: item["label"].casefold())
    if selected and selected not in stops:
        options.insert(0, {"value": selected, "label": selected})
    key = vol.Required("stop_id", default=selected) if selected else vol.Required("stop_id")
    fields = {
        key: selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=options, mode=selector.SelectSelectorMode.DROPDOWN
            )
        )
    }
    if calendar_days is not None:
        fields[vol.Required(CONF_CALENDAR_DAYS, default=calendar_days)] = selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=1, max=MAX_CALENDAR_DAYS, step=1, mode=selector.NumberSelectorMode.BOX
            )
        )
    return vol.Schema(fields)


def _duplicate(hass, stop_id, current_id=None):
    return any(
        configured_stop(entry) == stop_id and entry.entry_id != current_id
        for entry in hass.config_entries.async_entries(DOMAIN)
    )


class _StopForm:
    _stops = None

    async def _async_stops(self):
        if not self._stops:
            store = get_store(self.hass)
            await store.async_refresh()
            self._stops = store.parser.get_stops()
        return self._stops


class ZimSlupskConfigFlow(_StopForm, config_entries.ConfigFlow, domain=DOMAIN):
    """Keep version/data unchanged; no migration of existing entries is necessary."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        # config_entry is provided by HA as a read-only property after construction.
        return ZimSlupskOptionsFlow()

    async def _async_step_stop(self, step_id, user_input, entry=None):
        errors = {}
        selected = configured_stop(entry) if entry else None
        try:
            stops = await self._async_stops()
            if not stops:
                errors["base"] = "stops_data_unavailable"
            elif user_input is not None:
                stop_id = user_input.get("stop_id")
                selected = stop_id
                if stop_id not in stops:
                    errors["stop_id"] = "invalid_stop"
                elif _duplicate(self.hass, stop_id, entry.entry_id if entry else None):
                    errors["base"] = "already_configured"
                elif entry:
                    options = dict(entry.options)
                    if "stop_id" in options:
                        options["stop_id"] = stop_id
                    if entry.update_listeners:
                        # Older HA: the existing listener owns the reload.
                        self.hass.config_entries.async_update_entry(
                            entry, data={**entry.data, "stop_id": stop_id}, options=options
                        )
                        return self.async_abort(reason="reconfigure_successful")
                    return self.async_update_reload_and_abort(
                        entry, data_updates={"stop_id": stop_id}, options=options
                    )
                else:
                    stop = stops[stop_id]
                    title = f"ZIM Słupsk - {stop.get('stop_name', 'Nieznany')} {stop.get('stop_code', '').split('/')[-1]}"
                    return self.async_create_entry(title=title, data={"stop_id": stop_id})
        except ConfigEntryNotReady:
            errors["base"] = "cannot_connect"
            stops = {}
        return self.async_show_form(
            step_id=step_id, data_schema=_schema(stops, selected), errors=errors
        )

    async def async_step_user(self, user_input=None):
        return await self._async_step_stop("user", user_input)

    async def async_step_reconfigure(self, user_input=None):
        return await self._async_step_stop("reconfigure", user_input, self._get_reconfigure_entry())


class ZimSlupskOptionsFlow(_StopForm, OptionsFlowBase):
    async def async_step_init(self, user_input=None):
        errors = {}
        entry = self.config_entry
        selected = configured_stop(entry)
        calendar_days = entry.options.get(CONF_CALENDAR_DAYS, DEFAULT_CALENDAR_DAYS)
        try:
            calendar_days = validate_calendar_days(calendar_days)
        except ValueError:
            calendar_days = DEFAULT_CALENDAR_DAYS
        try:
            stops = await self._async_stops()
            if not stops:
                errors["base"] = "stops_data_unavailable"
            elif user_input is not None:
                stop_id = user_input.get("stop_id")
                selected = stop_id
                try:
                    calendar_days = validate_calendar_days(
                        user_input.get(CONF_CALENDAR_DAYS, calendar_days)
                    )
                except ValueError:
                    errors[CONF_CALENDAR_DAYS] = "invalid_calendar_days"
                if stop_id not in stops:
                    errors["stop_id"] = "invalid_stop"
                elif _duplicate(self.hass, stop_id, entry.entry_id):
                    errors["base"] = "already_configured"
                elif not errors:
                    return self.async_create_entry(
                        title="",
                        data={
                            **entry.options,
                            "stop_id": stop_id,
                            CONF_CALENDAR_DAYS: calendar_days,
                        },
                    )
        except ConfigEntryNotReady:
            errors["base"] = "cannot_connect"
            stops = {}
        return self.async_show_form(
            step_id="init", data_schema=_schema(stops, selected, calendar_days), errors=errors
        )
