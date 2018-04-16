import logging
from enum import Enum
from tzlocal import get_localzone
import pytz
import datetime as dt
from collections import OrderedDict

ME_RESOURCE = 'me'
USERS_RESOURCE = 'users'

NEXT_LINK_KEYWORD = '@odata.nextLink'

log = logging.getLogger(__name__)

MAX_RECIPIENTS_PER_MESSAGE = 500  # Actual limit on Office 365


class WellKnowFolderNames(Enum):
    INBOX = 'Inbox'
    JUNK = 'JunkEmail'
    DELETED = 'DeletedItems'
    DRAFTS = 'Drafts'
    SENT = 'SentItems'
    OUTBOX = 'Outbox'


class ChainOperator(Enum):
    AND = 'and'
    OR = 'or'


class ApiComponent:
    """ Base class for all object interactions with the Cloud Service API

    Exposes common access methods to the api protocol within all Api objects
    """

    _cloud_data_key = '__cloud_data__'  # wrapps cloud data with this dict key
    _endpoints = {}  # dict of all API service endpoints needed

    def __init__(self, *, protocol=None, main_resource=None, **kwargs):
        """ Object initialization
        :param protocol: A protocol class or instance to be used with this connection
        :param main_resource: main_resource to be used in these API comunications
        :param kwargs: Extra arguments
        """
        self.protocol = protocol() if isinstance(protocol, type) else protocol
        if self.protocol is None:
            raise ValueError('Protocol not provided to Api Component')
        self.main_resource = self._parse_resource(main_resource or protocol.default_resource)
        self._base_url = '{}{}'.format(self.protocol.service_url, self.main_resource)
        super().__init__(**kwargs)

    @staticmethod
    def _parse_resource(resource):
        """ Parses and completes resource information """
        if resource == ME_RESOURCE:
            return resource
        elif resource == USERS_RESOURCE:
            return resource
        else:
            if USERS_RESOURCE not in resource:
                resource = resource.replace('/', '')
                return '{}/{}'.format(USERS_RESOURCE, resource)
            else:
                return resource

    def build_url(self, endpoint):
        """ Returns a url for a given endpoint using the protocol service url """
        return '{}{}'.format(self._base_url, endpoint)

    def _gk(self, keyword):
        """ Alias for protocol.get_service_keyword """
        return self.protocol.get_service_keyword(keyword)

    def _cc(self, dict_key):
        """ Alias for protocol.convert_case """
        return self.protocol.convert_case(dict_key)

    def new_query(self, attribute=None):
        return Query(attribute=attribute, protocol=self.protocol)


class Pagination(ApiComponent):
    """ Utility class that allows batching requests to the server """

    def __init__(self, *, parent=None, data=None, constructor=None, next_link=None, limit=None):
        """
        Returns an iterator that returns data until it's exhausted. Then will request more data
        (same amount as the original request) to the server until this data is exhausted as well.
        Stops when no more data exists or limit is reached.

        :param parent: the parent class. Must implement attributes:
            con, api_version, main_resource, auth_method
        :param data: the start data to be return
        :param constructor: the data constructor for the next batch
        :param next_link: the link to request more data to
        :param limit: when to stop retrieving more data
        """
        if parent is None:
            raise ValueError('Parent must be another Api Component')

        super().__init__(protocol=parent.protocol, main_resource=parent.main_resource)

        self.con = parent.con
        self.constructor = constructor
        self.next_link = next_link
        self.limit = limit
        self.data = data if data else []

        data_count = len(data)
        if limit and limit < data_count:
            self.data_count = limit
            self.total_count = limit
        else:
            self.data_count = data_count
            self.total_count = data_count
        self.state = 0

    def __str__(self):
        return "'{}' Iterator".format(self.constructor.__name__ if self.constructor else 'Unknown')

    def __repr__(self):
        return self.__str__()

    def __bool__(self):
        return bool(self.data) or bool(self.next_link)

    def __iter__(self):
        return self

    def __next__(self):
        if self.state < self.data_count:
            value = self.data[self.state]
            self.state += 1
            return value
        else:
            if self.limit and self.total_count >= self.limit:
                raise StopIteration()

        if self.next_link is None:
            raise StopIteration()

        try:
            response = self.con.get(self.next_link)
        except Exception as e:
            log.error('Error while Paginating. Error: {}'.format(str(e)))
            raise e

        if response.status_code != 200:
            log.debug('Failed Request while Paginating. Reason: {}'.format(response.reason))
            raise StopIteration()

        data = response.json()
        self.next_link = data.get(NEXT_LINK_KEYWORD, None) or None
        data = data.get('value', [])
        if self.constructor:
            # Everything received from the cloud must be passed with self._cloud_data_key
            self.data = [self.constructor(parent=self, **{self._cloud_data_key: value})
                         for value in data]
        else:
            self.data = data

        items_count = len(data)
        if self.limit:
            dif = self.limit - (self.total_count + items_count)
            if dif < 0:
                self.data = self.data[:dif]
                self.next_link = None  # stop batching
                items_count = items_count + dif
        if items_count:
            self.data_count = items_count
            self.total_count += items_count
            self.state = 0
            value = self.data[self.state]
            self.state += 1
            return value
        else:
            raise StopIteration()


class Query:
    """ Helper to conform OData filters """
    _mapping = {
        'from': 'from/emailAddress/address',
        'to': 'toRecipients/emailAddress/address'
    }

    def __init__(self, attribute=None, *, protocol):
        self.protocol = protocol() if isinstance(protocol, type) else protocol
        self._attribute = None
        self._chain = None
        self.new(attribute)
        self._negation = False
        self._filters = []
        self._localtz = None  # lazy attribute
        self._order_by = OrderedDict()

    def __str__(self):
        return 'Filters: {}\nOrder: {}'.format(self.get_filters(), self.get_order())

    def __repr__(self):
        return self.__str__()

    def as_params(self):
        """ Returns the filters and orders as query parameters"""
        params = {}
        if self.has_filters():
            params['$filter'] = self.get_filters()
        if self.has_order():
            params['$orderby'] = self.get_order()
        return params

    def has_filters(self):
        return bool(self._filters)

    def has_order(self):
        return bool(self._order_by)

    def get_filters(self):
        """ Returns the result filters """
        if self._filters:
            filters_list = self._filters
            if isinstance(filters_list[-1], Enum):
                filters_list = filters_list[:-1]
            return ' '.join([fs.value if isinstance(fs, Enum) else fs[1] for fs in filters_list]).strip()
        else:
            return None

    def get_order(self):
        """ Returns the result order by clauses """
        # first get the filtered attributes in order as they must appear in the order_by first
        filter_order_clauses = OrderedDict([(filter_attr[0], None)
                                            for filter_attr in self._filters
                                            if isinstance(filter_attr, tuple)])

        # any order_by attribute that appears in the filters is is ignored
        for filter_oc in filter_order_clauses.keys():
            direction = self._order_by.pop(filter_oc, None)
            filter_order_clauses[filter_oc] = direction

        filter_order_clauses.update(self._order_by)  # append any remaining order_by clause

        if filter_order_clauses:
            return ','.join(['{} {}'.format(attribute, direction if direction else '').strip()
                             for attribute, direction in filter_order_clauses.items()])
        else:
            return None

    @property
    def localtz(self):
        """ Returns the cached local time zone """
        if self._localtz is None:
            self._localtz = get_localzone()
        return self._localtz

    def _get_mapping(self, attribute):
        if attribute:
            mapping = self._mapping.get(attribute)
            if mapping:
                attribute = '/'.join([self.protocol.convert_case(step) for step in mapping.split('/')])
            else:
                attribute = self.protocol.convert_case(attribute)
            return attribute
        return None

    def new(self, attribute, operation=ChainOperator.AND):
        if isinstance(operation, str):
            operation = ChainOperator(operation)
        self._chain = operation
        self._attribute = self._get_mapping(attribute) if attribute else None
        self._negation = False
        return self

    def clear(self):
        self._filters = []
        self._order_by = OrderedDict()
        self.new(None)
        return self

    def negate(self):
        self._negation = not self._negation
        return self

    def chain(self, operation=ChainOperator.AND):
        if isinstance(operation, str):
            operation = ChainOperator(operation)
        self._chain = operation
        return self

    def on_attribute(self, attribute):
        self._attribute = self._get_mapping(attribute)
        return self

    def _add_filter(self, filter_str):
        if self._attribute:
            if self._filters and not isinstance(self._filters[-1], ChainOperator):
                self._filters.append(self._chain)
            self._filters.append((self._attribute, filter_str))
        else:
            raise ValueError('Attribute property needed. call on_attribute(attribute) or new(attribute)')

    def _parse_filter_word(self, word):
        """ Converts the word parameter into the correct format """
        if isinstance(word, str):
            word = "'{}'".format(word)
        elif isinstance(word, dt.date):
            if isinstance(word, dt.datetime):
                if word.tzinfo is None:
                    # if it's a naive datetime, localize the datetime.
                    word = self.localtz.localize(word)  # localize datetime into local tz
                    word = word.astimezone(pytz.utc)  # transform local datetime to utc
            word = "'{}'".format(word.isoformat())  # convert datetime utc to isoformat
        elif isinstance(word, bool):
            word = str(word).lower()
        return word

    def logical_operator(self, operation, word):
        word = self._parse_filter_word(word)
        sentence = '{} {} {} {}'.format('not' if self._negation else '', self._attribute, operation, word).strip()
        self._add_filter(sentence)
        return self

    def equals(self, word):
        return self.logical_operator('eq', word)

    def unequal(self, word):
        return self.logical_operator('ne', word)

    def greater(self, word):
        return self.logical_operator('gt', word)

    def greater_equal(self, word):
        return self.logical_operator('ge', word)

    def less(self, word):
        return self.logical_operator('lt', word)

    def less_equal(self, word):
        return self.logical_operator('le', word)

    def function(self, function_name, word):
        word = self._parse_filter_word(word)

        self._add_filter(
            "{} {}({}, {})".format('not' if self._negation else '', function_name, self._attribute, word).strip())
        return self

    def contains(self, word):
        return self.function('contains', word)

    def startswith(self, word):
        return self.function('startswith', word)

    def endswith(self, word):
        return self.function('endswith', word)

    def order_by(self, attribute=None, *, ascending=True):
        """ applies a order_by clause"""
        attribute = self._get_mapping(attribute) or self._attribute
        if attribute:
            self._order_by[attribute] = None if ascending else 'desc'
        else:
            raise ValueError('Attribute property needed. call on_attribute(attribute) or new(attribute)')
        return self
