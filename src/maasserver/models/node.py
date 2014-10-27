# Copyright 2012-2014 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""Node objects."""

from __future__ import (
    absolute_import,
    print_function,
    unicode_literals,
    )

str = None

__metaclass__ = type
__all__ = [
    "Node",
    "fqdn_is_duplicate",
    "nodegroup_fqdn",
    ]


from collections import (
    defaultdict,
    namedtuple,
    )
from datetime import (
    datetime,
    timedelta,
    )
from itertools import chain
import re
from string import whitespace
from uuid import uuid1

import crochet
from django.contrib.auth.models import User
from django.core.exceptions import (
    PermissionDenied,
    ValidationError,
    )
from django.db import transaction
from django.db.models import (
    BooleanField,
    CharField,
    ForeignKey,
    IntegerField,
    Manager,
    ManyToManyField,
    Q,
    SET_DEFAULT,
    TextField,
    )
from django.shortcuts import get_object_or_404
import djorm_pgarray.fields
from maasserver import DefaultMeta
from maasserver.clusterrpc.dhcp import (
    remove_host_maps,
    update_host_maps,
    )
from maasserver.clusterrpc.power import (
    power_off_nodes,
    power_on_nodes,
    )
from maasserver.enum import (
    NODE_BOOT,
    NODE_BOOT_CHOICES,
    NODE_PERMISSION,
    NODE_STATUS,
    NODE_STATUS_CHOICES,
    NODE_STATUS_CHOICES_DICT,
    NODEGROUPINTERFACE_MANAGEMENT,
    POWER_STATE,
    POWER_STATE_CHOICES,
    PRESEED_TYPE,
    )
from maasserver.exceptions import (
    NodeStateViolation,
    StaticIPAddressTypeClash,
    )
from maasserver.fields import (
    JSONObjectField,
    MAC,
    )
from maasserver.models.candidatename import gen_candidate_names
from maasserver.models.cleansave import CleanSave
from maasserver.models.config import Config
from maasserver.models.dhcplease import DHCPLease
from maasserver.models.licensekey import LicenseKey
from maasserver.models.macaddress import (
    MACAddress,
    update_mac_cluster_interfaces,
    )
from maasserver.models.staticipaddress import StaticIPAddress
from maasserver.models.tag import Tag
from maasserver.models.timestampedmodel import TimestampedModel
from maasserver.models.zone import Zone
from maasserver.node_status import (
    COMMISSIONING_LIKE_STATUSES,
    get_failed_status,
    is_failed_status,
    NODE_TRANSITIONS,
    )
from maasserver.rpc import getClientFor
from maasserver.utils import (
    get_db_state,
    strip_domain,
    )
from netaddr import IPAddress
from piston.models import Token
from provisioningserver.logger import get_maas_logger
from provisioningserver.power.poweraction import UnknownPowerType
from provisioningserver.rpc.cluster import (
    CancelMonitor,
    StartMonitors,
    )
from provisioningserver.rpc.exceptions import MultipleFailures
from provisioningserver.rpc.power import QUERY_POWER_TYPES
from provisioningserver.utils.enum import map_enum_reverse
from provisioningserver.utils.twisted import asynchronous
from twisted.internet.defer import DeferredList
from twisted.protocols import amp


maaslog = get_maas_logger("node")


def wait_for_power_commands(deferreds):
    """Wait for a collection of power command deferreds to return or fail.

    :param deferreds: A collection of deferreds upon which to wait.
    :raises: MultipleFailures if any of the deferreds fail.
    """
    @asynchronous(timeout=30)
    def block_until_commands_complete():
        return DeferredList(deferreds, consumeErrors=True)

    results = block_until_commands_complete()

    failures = list(
        result for success, result in results if not success)

    if len(failures) != 0:
        raise MultipleFailures(*failures)


def generate_node_system_id():
    return 'node-%s' % uuid1()


def validate_hostname(hostname):
    """Validator for hostnames.

    :param hostname: Input value for a host name.  May include domain.
    :raise ValidationError: If the hostname is not valid according to RFCs 952
        and 1123.
    """
    # Valid characters within a hostname label: ASCII letters, ASCII digits,
    # hyphens, and underscores.  Not all are always valid.
    # Technically we could write all of this as a single regex, but it's not
    # very good for code maintenance.
    label_chars = re.compile('[a-zA-Z0-9_-]*$')

    if len(hostname) > 255:
        raise ValidationError(
            "Hostname is too long.  Maximum allowed is 255 characters.")
    # A hostname consists of "labels" separated by dots.
    labels = hostname.split('.')
    if '_' in labels[0]:
        # The host label cannot contain underscores; the rest of the name can.
        raise ValidationError(
            "Host label cannot contain underscore: %r." % labels[0])
    for label in labels:
        if len(label) == 0:
            raise ValidationError("Hostname contains empty name.")
        if len(label) > 63:
            raise ValidationError(
                "Name is too long: %r.  Maximum allowed is 63 characters."
                % label)
        if label.startswith('-') or label.endswith('-'):
            raise ValidationError(
                "Name cannot start or end with hyphen: %r." % label)
        if not label_chars.match(label):
            raise ValidationError(
                "Name contains disallowed characters: %r." % label)


# Return type from `get_effective_power_info`.
PowerInfo = namedtuple("PowerInfo", (
    "can_be_started", "can_be_stopped", "can_be_queried", "power_type",
    "power_parameters"))


class NodeManager(Manager):
    """A utility to manage the collection of Nodes."""

    def filter_by_ids(self, query, ids=None):
        """Filter `query` result set by system_id values.

        :param query: A QuerySet of Nodes.
        :type query: django.db.models.query.QuerySet_
        :param ids: Optional set of ids to filter by.  If given, nodes whose
            system_ids are not in `ids` will be ignored.
        :type param_ids: Sequence
        :return: A filtered version of `query`.

        .. _django.db.models.query.QuerySet: https://docs.djangoproject.com/
           en/dev/ref/models/querysets/

        """
        if ids is None:
            return query
        else:
            return query.filter(system_id__in=ids)

    def _filter_visible_nodes(self, nodes, user, perm):
        """Filter a `Node` query depending on user permissions.

        :param nodes: A `Node` query set.
        :param user: The user making the request; the filtering is based on
            their privileges.
        :param perm: Type of access requested.  For example, a user may be
            allowed to view some nodes that they are not allowed to edit.
        :type perm: `NODE_PERMISSION`
        :return: A version of `node` that is filtered to include only those
            nodes that `user` is allowed to access.
        """
        # If the data is corrupt, this can get called with None for
        # user where a Node should have an owner but doesn't.
        # Nonetheless, the code should not crash with corrupt data.
        if user is None:
            return nodes.none()
        if user.is_superuser:
            # Admin is allowed to see all nodes.
            return nodes
        elif perm == NODE_PERMISSION.VIEW:
            return nodes.filter(Q(owner__isnull=True) | Q(owner=user))
        elif perm == NODE_PERMISSION.EDIT:
            return nodes.filter(owner=user)
        elif perm == NODE_PERMISSION.ADMIN:
            return nodes.none()
        else:
            raise NotImplementedError(
                "Invalid permission check (invalid permission name: %s)." %
                perm)

    def get_nodes(self, user, perm, ids=None, from_nodes=None):
        """Fetch Nodes on which the User_ has the given permission.

        Warning: there could be a lot of nodes!  Keep scale in mind when
        calling this, and watch performance in general.  Prefetch related
        data where appropriate.

        :param user: The user that should be used in the permission check.
        :type user: User_
        :param perm: The permission to check.
        :type perm: a permission string from NODE_PERMISSION
        :param ids: If given, limit result to nodes with these system_ids.
        :type ids: Sequence.
        :param from_nodes: Optionally, restrict the answer to these nodes.
        :type from_nodes: Query set of `Node`.

        .. _User: https://
           docs.djangoproject.com/en/dev/topics/auth/
           #django.contrib.auth.models.User

        """
        if from_nodes is None:
            from_nodes = self.all()
        nodes = self._filter_visible_nodes(from_nodes, user, perm)
        return self.filter_by_ids(nodes, ids)

    def get_allocated_visible_nodes(self, token, ids):
        """Fetch Nodes that were allocated to the User_/oauth token.

        :param user: The user whose nodes to fetch
        :type user: User_
        :param token: The OAuth token associated with the Nodes.
        :type token: piston.models.Token.
        :param ids: Optional set of IDs to filter by. If given, nodes whose
            system_ids are not in `ids` will be ignored.
        :type param_ids: Sequence

        .. _User: https://
           docs.djangoproject.com/en/dev/topics/auth/
           #django.contrib.auth.models.User
        """
        if ids is None:
            nodes = self.filter(token=token)
        else:
            nodes = self.filter(token=token, system_id__in=ids)
        return nodes

    def get_node_or_404(self, system_id, user, perm):
        """Fetch a `Node` by system_id.  Raise exceptions if no `Node` with
        this system_id exist or if the provided user has not the required
        permission on this `Node`.

        :param name: The system_id.
        :type name: string
        :param user: The user that should be used in the permission check.
        :type user: django.contrib.auth.models.User
        :param perm: The permission to assert that the user has on the node.
        :type perm: unicode
        :raises: django.http.Http404_,
            :class:`maasserver.exceptions.PermissionDenied`.

        .. _django.http.Http404: https://
           docs.djangoproject.com/en/dev/topics/http/views/
           #the-http404-exception
        """
        node = get_object_or_404(Node, system_id=system_id)
        if user.has_perm(perm, node):
            return node
        else:
            raise PermissionDenied()

    def get_available_nodes_for_acquisition(self, for_user):
        """Find the nodes that can be acquired by the given user.

        :param for_user: The user who is to acquire the node.
        :type for_user: :class:`django.contrib.auth.models.User`
        :return: Those nodes which can be acquired by the user.
        :rtype: `django.db.models.query.QuerySet`
        """
        available_nodes = self.get_nodes(for_user, NODE_PERMISSION.VIEW)
        return available_nodes.filter(status=NODE_STATUS.READY)

    def stop_nodes(self, ids, by_user, stop_mode='hard'):
        """Request on given user's behalf that the given nodes be shut down.

        Shutdown is only requested for nodes that the user has ownership
        privileges for; any other nodes in the request are ignored.

        :param ids: The `system_id` values for nodes to be shut down.
        :type ids: Sequence
        :param by_user: Requesting user.
        :type by_user: User_
        :param stop_mode: Power off mode - usually 'soft' or 'hard'.
        :type stop_mode: unicode
        :return: Those Nodes for which shutdown was actually requested.
        :rtype: list
        """
        # Obtain node model objects for each node specified.
        nodes = self.get_nodes(by_user, NODE_PERMISSION.EDIT, ids=ids)

        # Helper function to whittle the list of nodes down to those that we
        # can actually stop, and keep hold of their power control info.
        def gen_power_info(nodes):
            for node in nodes:
                power_info = node.get_effective_power_info()
                if power_info.can_be_stopped:
                    # Smuggle in a hint about how to power-off the node.
                    power_info.power_parameters['power_off_mode'] = stop_mode
                    yield node, power_info

        # Create info that we can pass into the reactor (no model objects).
        nodes_stop_info = list(
            (node.system_id, node.hostname, node.nodegroup.uuid, power_info)
            for node, power_info in gen_power_info(nodes))
        powered_systems = [
            system_id for system_id, _, _, _ in nodes_stop_info]

        # Request that these nodes be powered off and wait for the
        # commands to return or fail.
        deferreds = power_off_nodes(nodes_stop_info).viewvalues()
        wait_for_power_commands(deferreds)

        # Return a list of those nodes that we've sent power commands for.
        return list(
            node for node in nodes if node.system_id in powered_systems)

    def start_nodes(self, ids, by_user, user_data=None):
        """Request on given user's behalf that the given nodes be started up.

        Power-on is only requested for nodes that the user has ownership
        privileges for; any other nodes in the request are ignored.

        Nodes are also ignored if they don't have a valid power type
        configured.

        :param ids: The `system_id` values for nodes to be started.
        :type ids: Sequence
        :param by_user: Requesting user.
        :type by_user: User_
        :param user_data: Optional blob of user-data to be made available to
            the nodes through the metadata service.  If not given, any
            previous user data is used.
        :type user_data: unicode
        :return: Those Nodes for which power-on was actually requested.
        :rtype: list

        :raises MultipleFailures: When there are failures originating from a
            remote process. There could be one or more failures -- it's not
            strictly *multiple* -- but they do all originate from comms with
            remote processes.
        :raises: `StaticIPAddressExhaustion` if there are not enough IP
            addresses left in the static range..
        """
        # Avoid circular imports.
        from metadataserver.models import NodeUserData

        # Obtain node model objects for each node specified.
        nodes = self.get_nodes(by_user, NODE_PERMISSION.EDIT, ids=ids)

        # Record the same user data for all nodes we've been *requested* to
        # start, regardless of whether or not we actually can; the user may
        # choose to manually start them.
        NodeUserData.objects.bulk_set_user_data(nodes, user_data)

        # Claim static IP addresses for all nodes we've been *requested* to
        # start, such that they're recorded in the database. This results in a
        # mapping of nodegroups to (ips, macs).
        static_mappings = defaultdict(dict)
        for node in nodes:
            if node.status == NODE_STATUS.ALLOCATED:
                claims = node.claim_static_ip_addresses()
                # If the PXE mac is on a managed interface then we can ask
                # the cluster to generate the DHCP host map(s).
                if node.is_pxe_mac_on_managed_interface():
                    static_mappings[node.nodegroup].update(claims)
                node.start_deployment()

        # XXX 2014-06-17 bigjools bug=1330765
        # If the above fails it needs to release the static IPs back to the
        # pool. An enclosing transaction or savepoint from the caller may take
        # care of this, given that a serious problem above will result in an
        # exception. If we're being belt-n-braces though it ought to clear up
        # before returning too. As part of the robustness work coming up, it
        # also needs to inform the user.

        # Update host maps and wait for them so that we can report failures
        # directly to the caller.
        update_host_maps_failures = list(update_host_maps(static_mappings))
        if len(update_host_maps_failures) != 0:
            raise MultipleFailures(*update_host_maps_failures)

        # Update the DNS zone with the new static IP info as necessary.
        from maasserver.dns.config import change_dns_zones
        change_dns_zones({node.nodegroup for node in nodes})

        # Helper function to whittle the list of nodes down to those that we
        # can actually start, and keep hold of their power control info.
        def gen_power_info(nodes):
            for node in nodes:
                power_info = node.get_effective_power_info()
                if power_info.can_be_started:
                    yield node, power_info

        # Create info that we can pass into the reactor (no model objects).
        nodes_start_info = list(
            (node.system_id, node.hostname, node.nodegroup.uuid, power_info)
            for node, power_info in gen_power_info(nodes))
        powered_systems = [
            system_id for system_id, _, _, _ in nodes_start_info]

        # Request that these nodes be powered off and wait for the
        # commands to return or fail.
        deferreds = power_on_nodes(nodes_start_info).viewvalues()
        wait_for_power_commands(deferreds)

        # Return a list of those nodes that we've sent power commands for.
        return list(
            node for node in nodes if node.system_id in powered_systems)


def patch_pgarray_types():
    """Monkey-patch incompatibility with recent versions of `djorm_pgarray`.

    An upstream commit in `djorm_pgarray` on 2013-07-21 effectively limits
    arrays to a fixed set of types.  An attempt to create an `ArrayField` of
    any other type results in the error "TypeError: invalid postgreSQL type."
    We have been getting that error with python-djorm-ext-pgarray 0.8, the
    first Ubuntu-packaged version, but not with 0.6.

    This function monkey-patches the set of supported types, adding macaddr.

    Upstream bug: https://github.com/niwibe/djorm-ext-pgarray/issues/19
    """
    # TYPES maps PostgreSQL type names to their Django casters.  The error
    # happens when using a postgres type name that is not in this dict.
    #
    # Older versions did not have TYPES, and worked out of the box.
    types_dict = getattr(djorm_pgarray.fields, 'TYPES', None)
    if types_dict is not None and 'macaddr' not in types_dict:
        djorm_pgarray.fields.TYPES['macaddr'] = MAC


# Monkey-patch djorm_pgarray's types list to support MAC.
patch_pgarray_types()


def nodegroup_fqdn(hostname, nodegroup_name):
    """Build a FQDN from a hostname and a nodegroup name.

    If hostname includes a domain, it is replaced with nodegroup_name.
    Otherwise, nodegroup name is append to hostname as a domain.
    """
    stripped_hostname = strip_domain(hostname)
    return '%s.%s' % (stripped_hostname, nodegroup_name)


def fqdn_is_duplicate(node, fqdn):
    """Determine if fqdn exists on any other nodes."""
    hostname = strip_domain(fqdn)
    nodes = Node.objects.filter(
        hostname__startswith=hostname).exclude(id=node.id)

    for check_node in nodes:
        if check_node.fqdn == fqdn:
            return True

    return False


# List of statuses for which it makes sense to release a node.
RELEASABLE_STATUSES = [
    NODE_STATUS.ALLOCATED,
    NODE_STATUS.RESERVED,
    NODE_STATUS.BROKEN,
    NODE_STATUS.DEPLOYING,
    NODE_STATUS.DEPLOYED,
    NODE_STATUS.FAILED_DEPLOYMENT,
    NODE_STATUS.FAILED_DISK_ERASING,
    NODE_STATUS.FAILED_RELEASING,
    ]


class Node(CleanSave, TimestampedModel):
    """A `Node` represents a physical machine used by the MAAS Server.

    :ivar system_id: The unique identifier for this `Node`.
        (e.g. 'node-41eba45e-4cfa-11e1-a052-00225f89f211').
    :ivar hostname: This `Node`'s hostname.  Must conform to RFCs 952 and 1123.
    :ivar status: This `Node`'s status. See the vocabulary
        :class:`NODE_STATUS`.
    :ivar error_description: A human-readable description of why a node is
        marked broken.  Only meaningful when the node is in the state 'BROKEN'.
    :ivar owner: This `Node`'s owner if it's in use, None otherwise.
    :ivar boot_type: This `Node`'s booting method. See the vocabulary
        :class:`NODE_BOOT`.
    :ivar osystem: This `Node`'s booting operating system, if it's blank then
        the default_osystem will be used.
    :ivar distro_series: This `Node`'s booting distro series, if
        it's blank then the default_distro_series will be used.
    :ivar power_type: The power type that determines how this
        node will be powered on. Its value must match a power driver template
        name.
    :ivar nodegroup: The `NodeGroup` this `Node` belongs to.
    :ivar tags: The list of :class:`Tag`s associated with this `Node`.
    :ivar objects: The :class:`NodeManager`.

    """

    class Meta(DefaultMeta):
        """Needed for South to recognize this model."""

    system_id = CharField(
        max_length=41, unique=True, default=generate_node_system_id,
        editable=False)

    hostname = CharField(
        max_length=255, default='', blank=True, unique=True,
        validators=[validate_hostname])

    status = IntegerField(
        max_length=10, choices=NODE_STATUS_CHOICES, editable=False,
        default=NODE_STATUS.DEFAULT)

    owner = ForeignKey(
        User, default=None, blank=True, null=True, editable=False)

    boot_type = CharField(
        max_length=20, choices=NODE_BOOT_CHOICES, default=NODE_BOOT.FASTPATH)

    osystem = CharField(
        max_length=20, blank=True, default='')

    distro_series = CharField(
        max_length=20, blank=True, default='')

    architecture = CharField(max_length=31, blank=False)

    routers = djorm_pgarray.fields.ArrayField(dbtype="macaddr")

    agent_name = CharField(max_length=255, default='', blank=True, null=True)

    error_description = TextField(blank=True, default='', editable=False)

    zone = ForeignKey(
        Zone, verbose_name="Physical zone",
        default=Zone.objects.get_default_zone, editable=True, db_index=True,
        on_delete=SET_DEFAULT)

    # Juju expects the following standard constraints, which are stored here
    # as a basic optimisation over querying the lshw output.
    cpu_count = IntegerField(default=0)
    memory = IntegerField(default=0)
    storage = IntegerField(default=0)

    # For strings, Django insists on abusing the empty string ("blank")
    # to mean "none."
    # The possible choices for this field depend on the power types
    # advertised by the clusters.  This needs to be populated on the fly,
    # in forms.py, each time the form to edit a node is instantiated.
    power_type = CharField(
        max_length=10, null=False, blank=True, default='')

    # JSON-encoded set of parameters for power control.
    power_parameters = JSONObjectField(blank=True, default="")

    power_state = CharField(
        max_length=10, null=False, blank=False,
        choices=POWER_STATE_CHOICES, default=POWER_STATE.UNKNOWN,
        editable=False)

    token = ForeignKey(
        Token, db_index=True, null=True, editable=False, unique=False)

    error = CharField(max_length=255, blank=True, default='')

    netboot = BooleanField(default=True)

    license_key = CharField(max_length=30, null=True, blank=True)

    # This field can't be null, but we can't enforce that in the
    # database schema because we can only create the default value from
    # a complete schema, after schema migration.  We can't use custom
    # model validation either, because the node forms need to set their
    # default values *after* saving the form (with commit=False), which
    # incurs validation before the default values are set.
    # So all we can do is set blank=False, and make the field editable
    # to cajole Django out of skipping it during "model" (actually model
    # form) validation.
    nodegroup = ForeignKey(
        'maasserver.NodeGroup', editable=True, null=True, blank=False)

    tags = ManyToManyField(Tag)

    # Disable IPv4 support on node once deployed, on operating systems that
    # support this choice.
    disable_ipv4 = BooleanField(
        default=False, verbose_name="Disable IPv4 when deployed",
        help_text=(
            "On operating systems where this choice is supported, this option "
            "disables IPv4 networking on this node when it is deployed.  "
            "IPv4 may still be used for booting and installing the node.  "
            "THIS MAY STOP YOUR NODE FROM WORKING.  Do not disable IPv4 "
            "unless you know what you're doing: clusters must be configured "
            "to use a MAAS URL with a hostname that resolves on both IPv4 and "
            "IPv6."))

    # Record the MAC address for the interface the node last PXE booted from.
    # This will be used for determining which MAC address to create a static
    # IP reservation for when starting a node.
    pxe_mac = ForeignKey(
        MACAddress, default=None, blank=True, null=True, editable=False,
        related_name='+')

    objects = NodeManager()

    def __unicode__(self):
        if self.hostname:
            return "%s (%s)" % (self.system_id, self.fqdn)
        else:
            return self.system_id

    @property
    def fqdn(self):
        """Fully qualified domain name for this node.

        If MAAS manages DNS for this node, the domain part of the
        hostname (if present), is replaced by the domain configured
        on the cluster controller.
        If not, simply return the node's hostname.
        """
        if self.nodegroup.manages_dns():
            return nodegroup_fqdn(self.hostname, self.nodegroup.name)
        return self.hostname

    def get_deployment_time(self):
        """Return the deployment time of this node (in seconds)."""
        # Return a *very* conservative estimate for now.
        # Something that shouldn't conflict with any deployment.
        return timedelta(minutes=40).total_seconds()

    def start_deployment(self):
        """Mark a node as being deployed."""
        self.status = NODE_STATUS.DEPLOYING
        self.save()
        # We explicitly commit here because during bulk node actions we
        # want to make sure that each successful state transition is
        # recorded in the DB.
        transaction.commit()
        deployment_time = self.get_deployment_time()
        self.start_transition_monitor(deployment_time)

    def end_deployment(self):
        """Mark a node as successfully deployed."""
        self.status = NODE_STATUS.DEPLOYED
        self.save()
        # We explicitly commit here because during bulk node actions we
        # want to make sure that each successful state transition is
        # recorded in the DB.
        transaction.commit()

    def start_transition_monitor(self, timeout):
        """Start cluster-side transition monitor."""
        context = {
            'node_status': self.status,
            'timeout': timeout,
            }
        deadline = datetime.now(tz=amp.utc) + timedelta(seconds=timeout)
        monitors = [{
            'deadline': deadline,
            'id': self.system_id,
            'context': context,
        }]
        client = getClientFor(self.nodegroup.uuid)
        call = client(StartMonitors, monitors=monitors)
        try:
            call.wait(5)
        except crochet.TimeoutError as error:
            maaslog.error(
                "%s: Unable to start transition monitor: %s",
                self.hostname, error)
        maaslog.info("%s: Starting monitor: %s", self.hostname, monitors[0])

    def stop_transition_monitor(self):
        """Stop cluster-side transition monitor."""
        client = getClientFor(self.nodegroup.uuid)
        call = client(CancelMonitor, id=self.system_id)
        try:
            call.wait(5)
        except crochet.TimeoutError as error:
            maaslog.error(
                "%s: Unable to stop transition monitor: %s",
                self.hostname, error)
        maaslog.info("%s: Stopping monitor: %s", self.hostname, self.system_id)

    def handle_monitor_expired(self, context):
        """Handle a monitor expired event."""
        failed_status = get_failed_status(self.status)
        if failed_status is not None:
            timeout_timedelta = timedelta(seconds=context['timeout'])
            self.mark_failed(
                "Node operation '%s' timed out after %s." % (
                    (
                        NODE_STATUS_CHOICES_DICT[self.status],
                        timeout_timedelta
                    )))

    def ip_addresses(self):
        """IP addresses allocated to this node.

        Return the current IP addresses for this Node, or the empty
        list if there are none.

        If `disable_ipv4` is set, any IPv4 addresses will be omitted.
        """
        # The static IP addresses are assigned/removed when a node is
        # allocated/deallocated.
        # The dynamic IP addresses are used by enlisting or commissioning
        # nodes.  This information is re-built periodically based on the
        # content of the DHCP lease file, the DB mappings can thus contain
        # outdated information for a short time.  They are returned here
        # for backward-compatiblity reasons (the static IP addresses were
        # introduced after the dynamic IP addresses) as only the static
        # mappings are guaranteed to be, well, static.
        ips = self.static_ip_addresses()
        if len(ips) == 0:
            ips = self.dynamic_ip_addresses()
        if self.disable_ipv4:
            return [
                ip
                for ip in ips
                if IPAddress(ip).version > 4
                ]
        else:
            return ips

    def static_ip_addresses(self):
        """Static IP addresses allocated to this node."""
        # If the macaddresses and the ips have been prefetched (a la
        # nodes = nodes.prefetch_related('macaddress_set__ip_addresses')),
        # use the cache.
        mac_cache = self.macaddress_set.all()._result_cache
        can_use_cache = (
            mac_cache is not None and
            (
                len(mac_cache) == 0
                or
                (
                    len(mac_cache) > 0 and
                    # If the first MAC has its IP addresses cached, assume
                    # we can use the cache for all the MACs.
                    mac_cache[0].ip_addresses.all()._result_cache is not None
                )
            )
        )
        if can_use_cache:
            # The cache is populated: return the static IP addresses of all the
            # node's MAC addresses.
            macs = self.macaddress_set.all()
            node_ip_addresses = [
                [ipaddr.ip for ipaddr in mac.ip_addresses.all()]
                for mac in macs
            ]
            return list(chain(*node_ip_addresses))
        else:
            return StaticIPAddress.objects.filter(
                macaddress__node=self).values_list('ip', flat=True)

    def dynamic_ip_addresses(self):
        """Dynamic IP addresses allocated to this node."""
        macs = [mac.mac_address for mac in self.macaddress_set.all()]
        dhcpleases_qs = self.nodegroup.dhcplease_set.all()
        if dhcpleases_qs._result_cache is not None:
            # If the dhcp lease set has been pre-fetched: use it to
            # extract the IP addresses associated with the nodes' MAC
            # addresses.
            return [lease.ip for lease in dhcpleases_qs if lease.mac in macs]
        else:
            query = dhcpleases_qs.filter(mac__in=macs)
            return query.values_list('ip', flat=True)

    def get_static_ip_mappings(self):
        """Return node's static addresses, and their MAC addresses.

        :return: A list of (IP, MAC) tuples, both in string form.
        """
        macs = self.macaddress_set.all().prefetch_related('ip_addresses')
        return [
            (sip.ip, mac.mac_address)
            for mac in macs
            for sip in mac.ip_addresses.all()
            ]

    def mac_addresses_on_managed_interfaces(self):
        """Return MACAddresses for this node that have a managed cluster
        interface."""
        # Avoid circular imports
        from maasserver.models import MACAddress
        unmanaged = NODEGROUPINTERFACE_MANAGEMENT.UNMANAGED
        return MACAddress.objects.filter(
            node=self, cluster_interface__isnull=False).exclude(
            cluster_interface__management=unmanaged)

    def tag_names(self):
        # We don't use self.tags.values_list here because this does not
        # take advantage of the cache.
        return [tag.name for tag in self.tags.all()]

    def clean_status(self):
        """Check a node's status transition against the node-status FSM."""
        old_status = get_db_state(self, 'status')
        if self.status == old_status:
            # No transition is always a safe transition.
            pass
        elif self.status in NODE_TRANSITIONS.get(old_status, ()):
            # Valid transition.
            if old_status is not None:
                stat = map_enum_reverse(NODE_STATUS, ignore=['DEFAULT'])
                maaslog.debug(
                    "%s: Transition status from %s to %s",
                    self.hostname, stat[old_status], stat[self.status])
            pass
        else:
            # Transition not permitted.
            error_text = "Invalid transition: %s -> %s." % (
                NODE_STATUS_CHOICES_DICT.get(old_status, "Unknown"),
                NODE_STATUS_CHOICES_DICT.get(self.status, "Unknown"),
                )
            raise NodeStateViolation(error_text)

    def clean(self, *args, **kwargs):
        super(Node, self).clean(*args, **kwargs)
        self.clean_status()

    def display_status(self):
        """Return status text as displayed to the user."""
        return NODE_STATUS_CHOICES_DICT[self.status]

    def display_memory(self):
        """Return memory in GB."""
        if self.memory < 1024:
            return '%.1f' % (self.memory / 1024.0)
        return '%d' % (self.memory / 1024)

    def display_storage(self):
        """Return storage in GB."""
        if self.storage < 1024:
            return '%.1f' % (self.storage / 1024.0)
        return '%d' % (self.storage / 1024)

    def add_mac_address(self, mac_address):
        """Add a new MAC address to this `Node`.

        :param mac_address: The MAC address to be added.
        :type mac_address: unicode
        :raises: django.core.exceptions.ValidationError_

        .. _django.core.exceptions.ValidationError: https://
           docs.djangoproject.com/en/dev/ref/exceptions/
           #django.core.exceptions.ValidationError
        """
        # Avoid circular imports
        from maasserver.models import MACAddress

        mac = MACAddress(mac_address=mac_address, node=self)
        mac.save()

        # See if there's a lease for this MAC and set its
        # cluster_interface if so.
        nodegroup_leases = {
            lease.ip: lease.mac
            for lease in DHCPLease.objects.filter(nodegroup=self.nodegroup)}
        update_mac_cluster_interfaces(nodegroup_leases, self.nodegroup)

        return mac

    def remove_mac_address(self, mac_address):
        """Remove a MAC address from this `Node`.

        :param mac_address: The MAC address to be removed.
        :type mac_address: string

        """
        # Avoid circular imports
        from maasserver.models import MACAddress

        mac = MACAddress.objects.get(mac_address=mac_address, node=self)
        if mac:
            mac.delete()

    def accept_enlistment(self, user):
        """Accept this node's (anonymous) enlistment.

        This call makes sense only on a node in New state, i.e. one that
        has been anonymously enlisted and is now waiting for a MAAS user to
        accept that enlistment as authentic.  Calling it on a node that is in
        Ready or Commissioning state, however, is not an error -- it probably
        just means that somebody else has beaten you to it.

        :return: This node if it has made the transition from New, or
            None if it was already in an accepted state.
        """
        accepted_states = [NODE_STATUS.READY, NODE_STATUS.COMMISSIONING]
        if self.status in accepted_states:
            return None
        if self.status != NODE_STATUS.NEW:
            raise NodeStateViolation(
                "Cannot accept node enlistment: node %s is in state %s."
                % (self.system_id, NODE_STATUS_CHOICES_DICT[self.status]))

        self.start_commissioning(user)
        return self

    def start_commissioning(self, user):
        """Install OS and self-test a new node."""
        # Avoid circular imports.
        from metadataserver.user_data.commissioning import generate_user_data
        from metadataserver.models import NodeResult

        commissioning_user_data = generate_user_data(node=self)
        NodeResult.objects.clear_results(self)
        # The commissioning profile is handled in start_nodes.
        maaslog.info(
            "%s: Starting commissioning", self.hostname)
        # We need to mark the node as COMMISSIONING now to avoid a race
        # when starting multiple nodes. We hang on to old_status just in
        # case the power action fails.
        old_status = self.status
        self.status = NODE_STATUS.COMMISSIONING
        self.save()
        transaction.commit()
        try:
            # We don't check for which nodes we've started here, because
            # it's possible we can't start the node - its power type may not
            # allow us to do that.
            Node.objects.start_nodes(
                [self.system_id], user, user_data=commissioning_user_data)
        except Exception as ex:
            maaslog.error(
                "%s: Unable to start node: %s",
                self.hostname, unicode(ex))
            self.status = old_status
            self.save()
            transaction.commit()
            # Let the exception bubble up, since the UI or API will have to
            # deal with it.
            raise
        else:
            maaslog.info("%s: Commissioning started", self.hostname)

    def abort_commissioning(self, user):
        """Power off a commissioning node and set its status to 'declared'."""
        if self.status != NODE_STATUS.COMMISSIONING:
            raise NodeStateViolation(
                "Cannot abort commissioning of a non-commissioning node: "
                "node %s is in state %s."
                % (self.system_id, NODE_STATUS_CHOICES_DICT[self.status]))
        maaslog.info(
            "%s: Aborting commissioning", self.hostname)
        try:
            # We don't check for which nodes we've stopped here, because
            # it's possible we can't stop the node - its power type may
            # not allow us to do that.
            Node.objects.stop_nodes([self.system_id], user)
        except Exception as ex:
            maaslog.error(
                "%s: Unable to shut node down: %s",
                self.hostname, unicode(ex))
            raise
        else:
            self.status = NODE_STATUS.NEW
            self.save()
            maaslog.info("%s: Commissioning aborted", self.hostname)

    def delete(self):
        """Delete this node.

        :raises MultipleFailures: If host maps cannot be deleted.
        """
        # Allocated nodes can't be deleted.
        if self.status == NODE_STATUS.ALLOCATED:
            raise NodeStateViolation(
                "Cannot delete node %s: node is in state %s."
                % (self.system_id, NODE_STATUS_CHOICES_DICT[self.status]))

        maaslog.info("%s: Deleting node", self.hostname)

        # Ensure that all static IPs are deleted, and keep track of the IP
        # addresses so we can delete the associated host maps.
        static_ips = StaticIPAddress.objects.delete_by_node(self)
        # Collect other IP addresses (likely in the dynamic range) that we
        # should delete host maps for. We need to do this because MAAS used to
        # declare host maps in the dynamic range. At some point we can stop
        # removing host maps from the dynamic range, once we decide that
        # enough time has passed.
        macs = self.mac_addresses_on_managed_interfaces().values_list(
            'mac_address', flat=True)
        leases = DHCPLease.objects.filter(
            nodegroup=self.nodegroup, mac__in=macs)
        leased_ips = leases.values_list("ip", flat=True)
        # Delete host maps for all addresses linked to this node.
        self.delete_host_maps(set().union(static_ips, leased_ips))
        # Delete the related mac addresses. The DHCPLease objects
        # corresponding to these MACs will be deleted as well. See
        # maasserver/models/dhcplease:delete_lease().
        self.macaddress_set.all().delete()

        super(Node, self).delete()

    def delete_host_maps(self, for_ips):
        """Delete any host maps for IPs allocated to this node.

        This should probably live on `NodeGroup`.

        :param for_ips: The set of IP addresses to remove host maps for.
        :type for_ips: `set`

        :raises MultipleFailures: When there are failures originating from a
            remote process. There could be one or more failures -- it's not
            strictly *multiple* -- but they do all originate from comms with
            remote processes.
        """
        assert isinstance(for_ips, set), "%r is not a set" % (for_ips,)
        if len(for_ips) > 0:
            maaslog.info("%s: Deleting DHCP host maps", self.hostname)
            removal_mapping = {self.nodegroup: for_ips}
            remove_host_maps_failures = list(
                remove_host_maps(removal_mapping))
            if len(remove_host_maps_failures) != 0:
                raise MultipleFailures(*remove_host_maps_failures)

    def set_random_hostname(self):
        """Set `hostname` from a shuffled list of candidate names.

        See `gen_candidate_names`.

        http://en.wikipedia.org/wiki/Hostname#Restrictions_on_valid_host_names
        """
        domain = Config.objects.get_config("enlistment_domain")
        domain = domain.strip("." + whitespace)
        for new_hostname in gen_candidate_names():
            if len(domain) > 0:
                self.hostname = "%s.%s" % (new_hostname, domain)
            else:
                self.hostname = "%s" % new_hostname
            try:
                self.save()
            except ValidationError:
                pass
            else:
                break

    def get_effective_power_type(self):
        """Get power-type to use for this node.

        If no power type has been set for the node, raise
        UnknownPowerType.
        """
        if self.power_type == '':
            raise UnknownPowerType("Node power type is unconfigured")
        return self.power_type

    def get_primary_mac(self):
        """Return the primary :class:`MACAddress` for this node."""
        macs = self.macaddress_set.order_by('created')[:1]
        if len(macs) > 0:
            return macs[0]
        else:
            return None

    def get_effective_kernel_options(self):
        """Determine any special kernel parameters for this node.

        :return: (tag, kernel_options)
            tag is a Tag object or None. If None, the kernel_options came from
            the global setting.
            kernel_options, a string indicating extra kernel_options that
            should be used when booting this node. May be None if no tags match
            and no global setting has been configured.
        """
        # First, see if there are any tags associated with this node that has a
        # custom kernel parameter
        tags = self.tags.filter(kernel_opts__isnull=False)
        tags = tags.order_by('name')
        for tag in tags:
            if tag.kernel_opts != '':
                return tag, tag.kernel_opts
        global_value = Config.objects.get_config('kernel_opts')
        return None, global_value

    @property
    def work_queue(self):
        """The name of the queue for tasks specific to this node."""
        return self.nodegroup.work_queue

    def get_osystem(self):
        """Return the operating system to install that node."""
        use_default_osystem = (self.osystem is None or self.osystem == '')
        if use_default_osystem:
            return Config.objects.get_config('default_osystem')
        else:
            return self.osystem

    def get_distro_series(self):
        """Return the distro series to install that node."""
        use_default_osystem = (
            self.osystem is None or
            self.osystem == '')
        use_default_distro_series = (
            self.distro_series is None or
            self.distro_series == '')
        if use_default_osystem and use_default_distro_series:
            return Config.objects.get_config('default_distro_series')
        else:
            return self.distro_series

    def get_effective_license_key(self):
        """Return effective license key.

        This returns the license key that should be used during the
        installation of the operating system for this node. This method first
        checks to see if the node has a specific license key set, if not then
        the license key registry is checked, if neither exists for this node or
        the booting operating system and release combination then an empty
        string is returned. An empty string can mean two things, one the
        operating system does not require a license key, or the installation
        media already has the license key builtin.
        """
        use_global_license_key = (
            self.license_key is None or
            self.license_key == '')
        if use_global_license_key:
            osystem = self.get_osystem()
            distro_series = self.get_distro_series()
            try:
                return LicenseKey.objects.get_license_key(
                    osystem, distro_series)
            except LicenseKey.DoesNotExist:
                return ''
        else:
            return self.license_key

    def get_effective_power_parameters(self):
        """Return effective power parameters, including any defaults."""
        if self.power_parameters:
            power_params = self.power_parameters.copy()
        else:
            # An empty power_parameters comes out as an empty unicode string!
            power_params = {}

        power_params.setdefault('system_id', self.system_id)
        # TODO: We should not be sending these paths to the templates;
        # the templates ought to know which tool to use themselves.
        power_params.setdefault('fence_cdu', '/usr/sbin/fence_cdu')
        power_params.setdefault('ipmipower', '/usr/sbin/ipmipower')
        power_params.setdefault('ipmitool', '/usr/bin/ipmitool')
        power_params.setdefault(
            'ipmi_chassis_config', '/usr/sbin/ipmi-chassis-config')
        power_params.setdefault('ipmi_config', 'ipmi.conf')
        # TODO: /end of paths that templates should know.
        # TODO: This default ought to be in the virsh template.
        if self.power_type == "virsh":
            power_params.setdefault(
                'power_address', 'qemu://localhost/system')
        else:
            power_params.setdefault('power_address', "")
        power_params.setdefault('username', '')
        power_params.setdefault('power_id', self.system_id)
        power_params.setdefault('power_driver', '')
        power_params.setdefault('power_pass', '')
        power_params.setdefault('power_off_mode', '')

        # The "mac" parameter defaults to the node's primary MAC
        # address, but only if not already set.
        if 'mac_address' not in power_params:
            primary_mac = self.get_primary_mac()
            if primary_mac is not None:
                mac = primary_mac.mac_address.get_raw()
                power_params['mac_address'] = mac

        # boot_mode is something that tells the template whether this is
        # a PXE boot or a local HD boot.
        if self.status == NODE_STATUS.DEPLOYED:
            power_params['boot_mode'] = 'local'
        else:
            power_params['boot_mode'] = 'pxe'

        return power_params

    def get_effective_power_info(self):
        """Get information on how to control this node's power.

        Returns a ``(can-be-started, can-be-stopped, power-type,
        power-parameters)`` tuple, where ``can-be-started`` and
        ``can-be-stopped`` are hints, based on the power type and power
        parameters, whether it's even worth trying to control this node's
        power.

        Put another way, if ``can-be-started`` is `False`, the node almost
        certainly cannot be started. If it's `True`, then it may be possible
        to control this node's power, but there are *no* guarantees. The same
        goes for ``can-be-stopped``.

        :returns: :py:class:`PowerInfo` (a `namedtuple`)
        """
        power_params = self.get_effective_power_parameters()
        try:
            power_type = self.get_effective_power_type()
        except UnknownPowerType:
            maaslog.warning("%s: Unrecognised power type.", self.hostname)
            return PowerInfo(False, False, False, None, None)
        else:
            if power_type == 'ether_wake':
                mac = power_params.get('mac_address')
                can_be_started = (mac != '' and mac is not None)
                can_be_stopped = False
            else:
                can_be_started = True
                can_be_stopped = True
            can_be_queried = power_type in QUERY_POWER_TYPES
            return PowerInfo(
                can_be_started, can_be_stopped, can_be_queried,
                power_type, power_params,
            )

    def acquire(self, user, token=None, agent_name=''):
        """Mark commissioned node as acquired by the given user and token."""
        assert self.owner is None
        assert token is None or token.user == user
        self.status = NODE_STATUS.ALLOCATED
        self.owner = user
        self.agent_name = agent_name
        self.token = token
        self.save()
        maaslog.info("%s allocated to user %s", self.hostname, user.username)

    def start_disk_erasing(self, user):
        """Erase the disks on a node."""
        # Avoid circular imports.
        from metadataserver.user_data.disk_erasing import generate_user_data

        disk_erase_user_data = generate_user_data(node=self)
        maaslog.info(
            "%s: Starting disk erasure", self.hostname)
        # Change the status of the node now to avoid races when starting
        # nodes in bulk.
        self.status = NODE_STATUS.DISK_ERASING
        self.save()
        transaction.commit()
        try:
            Node.objects.start_nodes(
                [self.system_id], user, user_data=disk_erase_user_data)
        except Exception as ex:
            maaslog.error(
                "%s: Unable to start node: %s",
                self.hostname, unicode(ex))
            # We always mark the node as failed here, although we could
            # potentially move it back to the state it was in
            # previously. For now, though, this is safer, since it marks
            # the node as needing attention.
            self.status = NODE_STATUS.FAILED_DISK_ERASING
            self.save()
            transaction.commit()
            raise
        else:
            maaslog.info(
                "%s: Disk erasure started.", self.hostname)

    def abort_disk_erasing(self, user):
        """
        Power off disk erasing node and set its status to 'failed disk
        erasing'.
        """
        if self.status != NODE_STATUS.DISK_ERASING:
            raise NodeStateViolation(
                "Cannot abort disk erasing of a non disk erasing node: "
                "node %s is in state %s."
                % (self.system_id, NODE_STATUS_CHOICES_DICT[self.status]))
        maaslog.info(
            "%s: Aborting disk erasing", self.hostname)
        try:
            Node.objects.stop_nodes([self.system_id], user)
        except Exception as ex:
            maaslog.error(
                "%s: Unable to shut node down: %s",
                self.hostname, unicode(ex))
            raise
        else:
            self.status = NODE_STATUS.FAILED_DISK_ERASING
            self.save()

    def abort_operation(self, user):
        """Abort the current operation.
        This currently only supports aborting Disk Erasing.
        """
        if self.status == NODE_STATUS.DISK_ERASING:
            self.abort_disk_erasing(user)
            return

        raise NodeStateViolation(
            "Cannot abort in current state: "
            "node %s is in state %s."
            % (self.system_id, NODE_STATUS_CHOICES_DICT[self.status]))

    def release(self):
        """Mark allocated or reserved node as available again and power off.

        :raises MultipleFailures: If host maps cannot be deleted.
        """
        maaslog.info("%s: Releasing node", self.hostname)
        try:
            Node.objects.stop_nodes([self.system_id], self.owner)
        except Exception as ex:
            maaslog.error(
                "%s: Unable to shut node down: %s", self.hostname,
                unicode(ex))
            raise

        if self.power_state == POWER_STATE.OFF:
            # Node is already off.
            self.status = NODE_STATUS.READY
            self.owner = None
        elif self.get_effective_power_info().can_be_queried:
            # Controlled power type (one for which we can query the power
            # state): update_power_state() will take care of making the node
            # READY and unowned when the power is finally off.
            self.status = NODE_STATUS.RELEASING
        else:
            # Uncontrolled power type (one for which we can't query the power
            # state): mark the node ready.
            self.status = NODE_STATUS.READY
            self.owner = None
        self.token = None
        self.agent_name = ''
        self.set_netboot()
        self.osystem = ''
        self.distro_series = ''
        self.license_key = ''
        self.save()

        # Do these after updating the node to avoid creating deadlocks with
        # other node editing operations.
        deallocated_ips = StaticIPAddress.objects.deallocate_by_node(self)
        self.delete_host_maps(deallocated_ips)
        from maasserver.dns.config import change_dns_zones
        change_dns_zones([self.nodegroup])

        # We explicitly commit here because during bulk node actions we
        # want to make sure that each successful state transition is
        # recorded in the DB.
        transaction.commit()

    def release_or_erase(self):
        """Either release the node or erase the node then release it, depending
        on settings."""
        erase_on_release = Config.objects.get_config(
            'enable_disk_erasing_on_release')
        if erase_on_release:
            self.start_disk_erasing(self.owner)
            return

        self.release()

    def set_netboot(self, on=True):
        """Set netboot on or off."""
        maaslog.debug("%s: Turning on netboot for node", self.hostname)
        self.netboot = on
        self.save()

    def get_deployment_status(self):
        """Return a string repr of the deployment status of this node."""
        mapping = {
            NODE_STATUS.DEPLOYED: "Deployed",
            NODE_STATUS.DEPLOYING: "Deploying",
            NODE_STATUS.FAILED_DEPLOYMENT: "Failed deployment",
        }
        return mapping.get(self.status, "Not in deployment")

    def split_arch(self):
        """Return architecture and subarchitecture, as a tuple."""
        arch, subarch = self.architecture.split('/')
        return (arch, subarch)

    def mark_failed(self, error_description):
        """Mark this node as failed.

        The actual 'failed' state depends on the current status of the
        node.
        """
        new_status = get_failed_status(self.status)
        if new_status is not None:
            self.status = new_status
            self.error_description = error_description
            self.save()
            maaslog.error(
                "%s: Marking node failed: %s", self.hostname,
                error_description)
        elif is_failed_status(self.status):
            # Silently ignore a request to fail an already failed node.
            pass
        else:
            raise NodeStateViolation(
                "The status of the node is %s; this status cannot "
                "be transitioned to a corresponding failed status." %
                self.status)

    def mark_broken(self, error_description):
        """Mark this node as 'BROKEN'.

        If the node is allocated, release it first.
        """
        if self.status in RELEASABLE_STATUSES:
            self.release()
        # release() normally sets the status to RELEASING and leaves the
        # owner in place, override that here as we're broken.
        self.status = NODE_STATUS.BROKEN
        self.owner = None
        self.error_description = error_description
        self.save()

    def mark_fixed(self):
        """Mark a broken node as fixed and change its state to 'READY'."""
        if self.status != NODE_STATUS.BROKEN:
            raise NodeStateViolation(
                "Can't mark a non-broken node as 'Ready'.")
        maaslog.info("%s: Marking node fixed", self.hostname)
        self.status = NODE_STATUS.READY
        self.error_description = ''
        self.save()

    def update_power_state(self, power_state):
        """Update a node's power state """
        self.power_state = power_state
        mark_ready = (
            self.status == NODE_STATUS.RELEASING and
            power_state == POWER_STATE.OFF)
        if mark_ready:
            # Ensure the node is fully released after a successful power
            # down.
            self.status = NODE_STATUS.READY
            self.owner = None
        self.save()

    def claim_static_ip_addresses(self):
        """Assign static IPs to the node's PXE MAC.

        :returns: A list of ``(ip-address, mac-address)`` tuples.
        :raises: `StaticIPAddressExhaustion` if there are not enough IPs left.
        """
        mac = self.get_pxe_mac()

        if mac is None:
            return []

        # XXX 2014-10-09 jhobbs bug=1379370
        # It's not clear to me that this transaction needs to be here
        # since this doesn't allocate IP addresses across multiple
        # interfaces. This needs to be looked at some more when there is
        # more time.
        with transaction.atomic():
            try:
                static_ips = mac.claim_static_ips()
            except StaticIPAddressTypeClash:
                # There's already a non-AUTO IP.
                return []

            # Return a list instead of yielding mappings as they're ready
            # because it's all-or-nothing (hence the atomic context).
            return [(static_ip.ip, unicode(mac)) for static_ip in static_ips]

    def get_boot_purpose(self):
        """
        Return a suitable "purpose" for this boot, e.g. "install".
        """
        # XXX: allenap bug=1031406 2012-07-31: The boot purpose is
        # still in flux. It may be that there will just be an
        # "ephemeral" environment and an "install" environment, and
        # the differing behaviour between, say, enlistment and
        # commissioning - both of which will use the "ephemeral"
        # environment - will be governed by varying the preseed or PXE
        # configuration.
        if self.status in COMMISSIONING_LIKE_STATUSES:
            # It is commissioning or disk erasing. The environment (boot
            # images, kernel options, etc for erasing is the same as that
            # of commissioning.
            return "commissioning"
        elif self.status == NODE_STATUS.DEPLOYING:
            # Install the node if netboot is enabled,
            # otherwise boot locally.
            if self.netboot:
                # Avoid circular imports.
                from maasserver.preseed import get_preseed_type_for
                preseed_type = get_preseed_type_for(self)
                if preseed_type == PRESEED_TYPE.CURTIN:
                    return "xinstall"
                else:
                    return "install"
            else:
                return "local"
        elif self.status == NODE_STATUS.DEPLOYED:
            return "local"
        else:
            return "poweroff"

    def get_pxe_mac(self):
        """Get the MAC address this node is expected to PXE boot from.

        Normally, this will be the MAC address last used in a
        pxeconfig() API request for the node, as recorded in the
        'pxe_mac' property. However, if the node hasn't PXE booted since
        the 'pxe_mac' property was added to the Node model, this will
        return the node's first MAC address instead.
        """
        if self.pxe_mac is not None:
            return self.pxe_mac

        return self.macaddress_set.first()

    def is_pxe_mac_on_managed_interface(self):
        pxe_mac = self.get_pxe_mac()
        if pxe_mac is not None:
            cluster_interface = pxe_mac.cluster_interface
            if cluster_interface is not None:
                return cluster_interface.is_managed
        return False
