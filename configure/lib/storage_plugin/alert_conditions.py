

class AlertCondition(object):
    def __init__(self):
        self._name = None

    def set_name(self, name):
        self._name = name


class AttrValAlertCondition(AlertCondition):
    def __init__(self, attribute, error_states = [], warn_states = [], info_states = [], message = None, *args, **kwargs):
        self.error_states = error_states
        self.warn_states = warn_states
        self.info_states = info_states
        self.attribute = attribute
        self.message = message
        super(AttrValAlertCondition, self).__init__(*args, **kwargs)

    def alert_classes(self):
        result = []
        import logging
        states_sev = [
                (self.error_states, logging.ERROR),
                (self.warn_states, logging.WARNING),
                (self.info_states, logging.INFO)
                ]
        for states, sev in states_sev:
            if len(states) == 0:
                continue
            else:
                alert_name = "_%s_%s_%s" % (self._name, self.attribute, sev)
                result.append(alert_name)

        return result

    def test(self, resource):
        result = []
        import logging
        states_sev = [
                (self.error_states, logging.ERROR),
                (self.warn_states, logging.WARNING),
                (self.info_states, logging.INFO)
                ]
        for states, sev in states_sev:
            if len(states) == 0:
                continue
            # FIXME: nowhere to put the severity for an alert yet
            alert_name = "_%s_%s_%s" % (self._name, self.attribute, sev)
            active = getattr(resource, self.attribute) in states
            result.append([alert_name, self.attribute, active])

        return result
