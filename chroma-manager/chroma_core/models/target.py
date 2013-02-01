#
# ========================================================
# Copyright (c) 2012 Whamcloud, Inc.  All rights reserved.
# ========================================================


import json
import logging
import uuid
from chroma_core.lib.cache import ObjectCache
from django.contrib.contenttypes.models import ContentType

from django.db import models, transaction
from chroma_core.lib.job import  DependOn, DependAny, DependAll, Step, AnyTargetMountStep, job_log
from chroma_core.models.alert import AlertState
from chroma_core.models.event import AlertEvent
from chroma_core.models.jobs import StateChangeJob, StateLock, AdvertisedJob
from chroma_core.models.host import ManagedHost, LNetConfiguration, VolumeNode, Volume
from chroma_core.models.jobs import StatefulObject
from chroma_core.models.utils import DeletableMetaclass, DeletableDowncastableMetaclass, MeasuredEntity
import settings


class FilesystemMember(models.Model):
    """A Mountable for a particular filesystem, such as
       MDT, OST or Client"""
    filesystem = models.ForeignKey('ManagedFilesystem')
    index = models.IntegerField()

    # Use of abstract base classes to avoid django bug #12002
    class Meta:
        abstract = True


class ManagedTarget(StatefulObject):
    __metaclass__ = DeletableDowncastableMetaclass
    name = models.CharField(max_length = 64, null = True, blank = True,
            help_text = "Lustre target name, e.g. 'testfs-OST0001'.  May be null\
            if the target has not yet been registered.")

    uuid = models.CharField(max_length = 64, null = True, blank = True,
            help_text = "UUID of the target's internal filesystem.  May be null\
                    if the target has not yet been formatted")

    ha_label = models.CharField(max_length = 64, null = True, blank = True,
            help_text = "Label used for HA layer: human readable but unique")

    volume = models.ForeignKey('Volume')

    inode_size = models.IntegerField(null = True, blank = True)
    bytes_per_inode = models.IntegerField(null = True, blank = True)
    inode_count = models.IntegerField(null = True, blank = True)

    def primary_server(self):
        return self.managedtargetmount_set.get(primary = True).host

    def secondary_servers(self):
        return [tm.host for tm in self.managedtargetmount_set.filter(primary = False)]

    def role(self):
        if self.downcast_class == ManagedMdt:
            return "MDT"
        elif self.downcast_class == ManagedOst:
            return "OST"
        elif self.downcast_class == ManagedMgs:
            return "MGT"
        else:
            raise NotImplementedError()

    def update_active_mount(self, nodename):
        """Set the active_mount attribute from the nodename of a host, raising
        RuntimeErrors if the host doesn't exist or doesn't have a ManagedTargetMount"""
        try:
            started_on = ManagedHost.objects.get(nodename = nodename)
        except ManagedHost.DoesNotExist:
            raise RuntimeError("Target %s (%s) found on host %s, which is not a ManagedHost" % (self, self.id, nodename))
        try:
            job_log.debug("Started %s on %s" % (self.ha_label, started_on))
            self.active_mount = self.managedtargetmount_set.get(host = started_on)
            self.save()
        except ManagedTargetMount.DoesNotExist:
            job_log.error("Target %s (%s) found on host %s (%s), which has no ManagedTargetMount for this self" % (self, self.id, started_on, started_on.pk))
            raise RuntimeError("Target %s reported as running on %s, but it is not configured there" % (self, started_on))

    def get_param(self, key):
        params = self.targetparam_set.filter(key = key)
        return [p.value for p in params]

    def get_params(self):
        return [(p.key, p.value) for p in self.targetparam_set.all()]

    def get_failover_nids(self):
        fail_nids = []
        for secondary_mount in self.managedtargetmount_set.filter(primary = False):
            host = secondary_mount.host
            failhost_nids = host.lnetconfiguration.get_nids()
            assert(len(failhost_nids) != 0)
            fail_nids.extend(failhost_nids)
        return fail_nids

    @property
    def default_mount_point(self):
        return "/mnt/%s" % self.name

    @property
    def primary_host(self):
        return ManagedTargetMount.objects.get(target = self, primary = True).host

    @property
    def failover_hosts(self):
        return ManagedHost.objects.filter(managedtargetmount__target = self, managedtargetmount__primary = False)

    @property
    def active_host(self):
        if self.active_mount:
            return self.active_mount.host
        else:
            return None

    def get_label(self):
        return self.name

    def __str__(self):
        return self.name

    # unformatted: I exist in theory in the database
    # formatted: I've been mkfs'd
    # registered: I've registered with the MGS, I'm not setup in HA yet
    # unmounted: I'm set up in HA, ready to mount
    # mounted: Im mounted
    # removed: this target no longer exists in real life
    # forgotten: Equivalent of 'removed' for immutable_state targets
    # Additional states needed for 'deactivated'?
    states = ['unformatted', 'formatted', 'registered', 'unmounted', 'mounted', 'removed', 'forgotten']
    initial_state = 'unformatted'
    active_mount = models.ForeignKey('ManagedTargetMount', blank = True, null = True)

    def set_state(self, state, intentional = False):
        job_log.debug("mt.set_state %s %s" % (state, intentional))
        super(ManagedTarget, self).set_state(state, intentional)
        if intentional:
            TargetOfflineAlert.notify_quiet(self, self.state == 'unmounted')
        else:
            TargetOfflineAlert.notify(self, self.state == 'unmounted')

    class Meta:
        app_label = 'chroma_core'
        ordering = ['id']

    def get_deps(self, state = None):
        from chroma_core.models import ManagedFilesystem
        if not state:
            state = self.state

        deps = []
        if state == 'mounted' and self.active_mount and not self.immutable_state:
            # Depend on the active mount's host having LNet up, so that if
            # LNet is stopped on that host this target will be stopped first.
            target_mount = self.active_mount
            host = ObjectCache.get_one(ManagedHost, lambda mh: mh.id == target_mount.host_id)
            deps.append(DependOn(host, 'lnet_up', fix_state='unmounted'))

            # TODO: also express that this situation may be resolved by migrating
            # the target instead of stopping it.

        if isinstance(self, FilesystemMember) and state not in ['removed', 'forgotten']:
            # Make sure I follow if filesystem goes to 'removed'
            # or 'forgotten'
            filesystem = ObjectCache.get_one(ManagedFilesystem, lambda fs: fs.id == self.filesystem_id)
            deps.append(DependOn(filesystem, 'available',
                acceptable_states = filesystem.not_states(['forgotten', 'removed']), fix_state=lambda s: s))

        if state not in ['removed', 'forgotten']:
            target_mounts = ObjectCache.get(ManagedTargetMount, lambda mtm: mtm.target_id == self.id)
            for tm in target_mounts:
                if self.immutable_state:
                    deps.append(DependOn(tm.host, 'lnet_up', acceptable_states = list(set(tm.host.states) - set(['removed', 'forgotten'])), fix_state = 'forgotten'))
                else:
                    deps.append(DependOn(tm.host, 'lnet_up', acceptable_states = list(set(tm.host.states) - set(['removed', 'forgotten'])), fix_state = 'removed'))

        return DependAll(deps)

    reverse_deps = {
            'ManagedTargetMount': lambda mtm: ObjectCache.mtm_targets(mtm.id),
            'ManagedHost': lambda mh: ObjectCache.host_targets(mh.id),
            'ManagedFilesystem': lambda mfs: ObjectCache.fs_targets(mfs.id)
            }

    @classmethod
    def create_for_volume(cls, volume_id, create_target_mounts = True, **kwargs):
        # Local imports to avoid inter-model import dependencies
        volume = Volume.objects.get(pk = volume_id)

        target = cls(**kwargs)
        target.volume = volume

        # Acquire a target index for FilesystemMember targets, and
        # populate `name`
        if issubclass(cls, ManagedMdt):
            index = target.filesystem.mdt_next_index
            target.name = "%s-MDT%04x" % (target.filesystem.name, index)
            target.index = index
            target.filesystem.mdt_next_index += 1
            target.filesystem.save()
        elif issubclass(cls, ManagedOst):
            index = target.filesystem.ost_next_index
            target.name = "%s-OST%04x" % (target.filesystem.name, index)
            target.index = index
            target.filesystem.ost_next_index += 1
            target.filesystem.save()
        else:
            target.name = "MGS"

        target.save()

        def create_target_mount(volume_node):
            mount = ManagedTargetMount(
                volume_node = volume_node,
                target = target,
                host = volume_node.host,
                mount_point = target.default_mount_point,
                primary = volume_node.primary)
            mount.save()

        if create_target_mounts:
            try:
                primary_volume_node = volume.volumenode_set.get(primary = True, host__not_deleted = True)
                create_target_mount(primary_volume_node)
            except VolumeNode.DoesNotExist:
                raise RuntimeError("No primary lun_node exists for volume %s, cannot create target" % volume.id)
            except VolumeNode.MultipleObjectsReturned:
                raise RuntimeError("Multiple primary lun_nodes exist for volume %s, internal error" % volume.id)

            for secondary_volume_node in volume.volumenode_set.filter(use = True, primary = False, host__not_deleted = True):
                create_target_mount(secondary_volume_node)

        return target


class ManagedOst(ManagedTarget, FilesystemMember, MeasuredEntity):
    class Meta:
        app_label = 'chroma_core'
        ordering = ['id']

    def get_available_states(self, begin_state):
        # Exclude the transition to 'removed' in favour of being removed when our FS is
        if self.immutable_state:
            return []
        else:
            available_states = super(ManagedOst, self).get_available_states(begin_state)
            available_states = list(set(available_states) ^ set(['forgotten']))
            return available_states


class ManagedMdt(ManagedTarget, FilesystemMember, MeasuredEntity):
    class Meta:
        app_label = 'chroma_core'
        ordering = ['id']

    def get_available_states(self, begin_state):
        # Exclude the transition to 'removed' in favour of being removed when our FS is
        if self.immutable_state:
            return []
        else:
            available_states = super(ManagedMdt, self).get_available_states(begin_state)
            available_states = list(set(available_states) - set(['removed', 'forgotten']))

            return available_states


class ManagedMgs(ManagedTarget, MeasuredEntity):
    conf_param_version = models.IntegerField(default = 0)
    conf_param_version_applied = models.IntegerField(default = 0)

    def get_available_states(self, begin_state):
        if self.immutable_state:
            if self.managedfilesystem_set.count() == 0:
                return ['forgotten']
            else:
                return []
        else:
            available_states = super(ManagedMgs, self).get_available_states(begin_state)

            # Exclude the transition to 'forgotten' because immutable_state is False
            available_states = list(set(available_states) - set(['forgotten']))

            # Only advertise removal if the FS has already gone away
            if self.managedfilesystem_set.count() > 0:
                available_states = list(set(available_states) - set(['removed']))
                if 'removed' in available_states:
                    available_states.remove('removed')

            return available_states

    @classmethod
    def get_by_host(cls, host):
        return cls.objects.get(managedtargetmount__host = host)

    class Meta:
        app_label = 'chroma_core'
        ordering = ['id']

    def nids(self):
        """Return a list of NID strings"""
        nids = []
        # Note: order by -primary in order that the first argument passed to mkfs
        # in failover configurations is the primary mount -- Lustre will use the
        # first --mgsnode argument as the NID to connect to for target registration,
        # and if that is the secondary NID then bad things happen during first
        # filesystem start.
        for target_mount in self.managedtargetmount_set.all().order_by('-primary'):
            host = target_mount.host
            nids.extend(host.lnetconfiguration.get_nids())

        return nids

    def mgsnode_spec(self):
        """Return a list of strings of --mgsnode arguments suitable for use with mkfs"""
        result = []

        nids = ",".join(self.nids())
        assert(nids != "")
        result.append("--mgsnode=%s" % nids)

        return result

    def set_conf_params(self, params, new = True):
        """
        :param new: If False, do not increment the conf param version number, resulting in
                    new conf params not immediately being applied to the MGS (use if importing
                    records for an already configured filesystem).
        :param params: is a list of unsaved ConfParam objects"""
        version = None
        from django.db.models import F
        if new:
            ManagedMgs.objects.filter(pk = self.id).update(conf_param_version = F('conf_param_version') + 1)
        version = ManagedMgs.objects.get(pk = self.id).conf_param_version
        for p in params:
            p.version = version
            p.save()


class TargetRecoveryInfo(models.Model):
    """Record of what we learn from /proc/fs/lustre/*/*/recovery_status
       for a running target"""
    #: JSON-encoded dict parsed from /proc
    recovery_status = models.TextField()

    target = models.ForeignKey('chroma_core.ManagedTarget')

    class Meta:
        app_label = 'chroma_core'
        ordering = ['id']

    @staticmethod
    @transaction.commit_on_success
    def update(target, recovery_status):
        TargetRecoveryInfo.objects.filter(target = target).delete()
        instance = TargetRecoveryInfo.objects.create(
                target = target,
                recovery_status = json.dumps(recovery_status))
        return instance.is_recovering(recovery_status)

    def is_recovering(self, data = None):
        if not data:
            data = json.loads(self.recovery_status)
        return ("status" in data and data["status"] == "RECOVERING")

    #def recovery_status_str(self):
    #    data = json.loads(self.recovery_status)
    #    if 'status' in data and data["status"] == "RECOVERING":
    #        return "%s %ss remaining" % (data["status"], data["time_remaining"])
    #    elif 'status' in data:
    #        return data["status"]
    #    else:
    #        return "N/A"


class DeleteTargetStep(Step):
    idempotent = True

    def run(self, kwargs):
        target = kwargs['target']

        if issubclass(target.downcast_class, ManagedMgs):
            from chroma_core.models.filesystem import ManagedFilesystem
            assert ManagedFilesystem.objects.filter(mgs = target).count() == 0
        target.mark_deleted()

        if target.volume.storage_resource is None:
            # If a LogicalDrive storage resource goes away, but the
            # volume is in use by a target, then the volume is left behind.
            # Check if this is the case, and clean up any remaining volumes.
            for vn in VolumeNode.objects.filter(volume = target.volume):
                vn.mark_deleted()
            target.volume.mark_deleted()


class RemoveConfiguredTargetJob(StateChangeJob):
    state_transition = (ManagedTarget, 'unmounted', 'removed')
    stateful_object = 'target'
    state_verb = "Remove"
    target = models.ForeignKey(ManagedTarget)

    def get_requires_confirmation(self):
        return True

    def get_confirmation_string(self):
        if issubclass(self.target.downcast_class(), ManagedOst):
            return "Remove the OST from the file system. It will no longer be seen in Chroma Manager. Before removing the OST, manually remove all data from the OST. When an OST is removed, files stored on the OST will no longer be accessible."
        else:
            return None

    class Meta:
        app_label = 'chroma_core'
        ordering = ['id']

    def description(self):
        return "Remove target %s from configuration" % (self.target)

    def get_deps(self):
        deps = []

        return DependAll(deps)

    def get_steps(self):
        # TODO: actually do something with Lustre before deleting this from our DB
        steps = []
        for target_mount in self.target.managedtargetmount_set.all().order_by('primary'):
            steps.append((UnconfigurePacemakerStep, {'target_mount': target_mount}))
        steps.append((DeleteTargetStep, {'target': self.target}))
        return steps


# HYD-832: when transitioning from 'registered' to 'removed', do something to
# remove this target from the MGS
class RemoveTargetJob(StateChangeJob):
    class Meta:
        app_label = 'chroma_core'
        ordering = ['id']

    state_transition = (ManagedTarget, ['unformatted', 'formatted', 'registered'], 'removed')
    stateful_object = 'target'
    state_verb = "Remove"
    target = models.ForeignKey(ManagedTarget)

    def description(self):
        return "Remove target %s from configuration" % (self.target)

    def get_steps(self):
        return [(DeleteTargetStep, {'target': self.target})]

    def get_confirmation_string(self):
        if issubclass(self.target.downcast_class, ManagedOst):
            if self.target.state == 'registered':
                return "Remove the OST from the file system. It will no longer be seen in Chroma Manager. Before removing the OST, manually remove all data from the OST. When an OST is removed, files stored on the OST will no longer be accessible."
            else:
                return None
        else:
            return None

    def get_requires_confirmation(self):
        return True


class ForgetTargetJob(StateChangeJob):
    class Meta:
        app_label = 'chroma_core'
        ordering = ['id']

    def description(self):
        return "Remove unmanaged target %s" % self.target

    def get_steps(self):
        return [(DeleteTargetStep, {'target': self.target})]

    def get_requires_confirmation(self):
        return True

    state_transition = (ManagedTarget, ['unmounted', 'mounted'], 'forgotten')
    stateful_object = 'target'
    state_verb = "Remove"
    target = models.ForeignKey(ManagedTarget)


class RegisterTargetStep(Step):
    idempotent = True

    def run(self, kwargs):
        target = kwargs['target']
        target_mount = target.managedtargetmount_set.get(primary = True)

        mgs_id = target.downcast().filesystem.mgs.id
        mgs = ObjectCache.get_one(ManagedMgs, lambda t: t.id == mgs_id)

        # Check that the active mount of the MGS is its primary mount (HYD-233 Lustre limitation)
        if not mgs.active_mount == mgs.managedtargetmount_set.get(primary = True):
            raise RuntimeError("Cannot register target while MGS is not started on its primary server")

        result = self.invoke_agent(target_mount.host, "register_target",
            {'device': target_mount.volume_node.path,
             'mount_point': target_mount.mount_point})

        target = target_mount.target
        if not result['label'] == target.name:
            # We synthesize a target name (e.g. testfs-OST0001) when creating targets, then
            # pass --index to mkfs.lustre, so our name should match what is set after registration
            raise RuntimeError("Registration returned unexpected target name '%s' (expected '%s')" % (result['label'], target.name))
        target.save()
        job_log.debug("Registration complete, updating target %d with name=%s, ha_label=%s" % (target.id, target.name, target.ha_label))


class GenerateHaLabelStep(Step):
    idempotent = True

    def run(self, kwargs):
        target = kwargs['target']
        target.ha_label = "%s_%s" % (target.name, uuid.uuid4().__str__()[0:6])
        target.save()
        job_log.debug("Generated ha_label=%s for target %s (%s)" % (target.ha_label, target.id, target.name))


class ConfigurePacemakerStep(Step):
    idempotent = True

    def run(self, kwargs):
        target_mount = kwargs['target_mount']

        assert(target_mount.volume_node is not None)

        self.invoke_agent(target_mount.host, "configure_ha", {
                                    'device': target_mount.volume_node.path,
                                    'ha_label': target_mount.target.ha_label,
                                    'uuid': target_mount.target.uuid,
                                    'primary': target_mount.primary,
                                    'mount_point': target_mount.mount_point})


class UnconfigurePacemakerStep(Step):
    idempotent = True

    def run(self, kwargs):
        target_mount = kwargs['target_mount']

        self.invoke_agent(target_mount.host, "unconfigure_ha",
            {
                'ha_label': target_mount.target.ha_label,
                'uuid': target_mount.target.uuid,
                'primary': target_mount.primary
            })


class ConfigureTargetJob(StateChangeJob):
    state_transition = (ManagedTarget, 'registered', 'unmounted')
    stateful_object = 'target'
    state_verb = "Configure mount points"
    target = models.ForeignKey(ManagedTarget)

    class Meta:
        app_label = 'chroma_core'
        ordering = ['id']

    def description(self):
        return "Configure %s mount points" % self.target

    def get_steps(self):
        steps = []

        for target_mount in self.target.managedtargetmount_set.all().order_by('-primary'):
            steps.append((ConfigurePacemakerStep, {'target_mount': target_mount}))

        return steps

    def get_deps(self):
        deps = []

        prim_mtm = ObjectCache.get_one(ManagedTargetMount, lambda mtm: mtm.primary == True and mtm.target_id == self.target.id)
        deps.append(DependOn(prim_mtm.host, 'lnet_up'))

        return DependAll(deps)


class RegisterTargetJob(StateChangeJob):
    # FIXME: this really isn't ManagedTarget, it's FilesystemMember+ManagedTarget
    state_transition = (ManagedTarget, 'formatted', 'registered')
    stateful_object = 'target'
    state_verb = "Register"
    target = models.ForeignKey(ManagedTarget)

    class Meta:
        app_label = 'chroma_core'
        ordering = ['id']

    def description(self):
        return "Register %s" % self.target

    def get_steps(self):
        steps = []

        target_class = self.target.downcast_class
        if issubclass(target_class, ManagedMgs):
            steps = []
        if issubclass(target_class, FilesystemMember):
            steps = [(RegisterTargetStep, {'target': self.target})]

        steps.append((GenerateHaLabelStep, {'target': self.target}))

        return steps

    def get_deps(self):
        deps = []

        target = ObjectCache.get_one(self.target.downcast_class, lambda t: t.id == self.target.id)
        deps.append(DependOn(ObjectCache.target_primary_server(target), 'lnet_up'))

        if isinstance(target, FilesystemMember):
            mgs = ObjectCache.get_one(ManagedMgs, lambda t: t.id == target.filesystem.mgs_id)

            deps.append(DependOn(mgs, "mounted"))

        if isinstance(target, ManagedOst):
            mdts = ObjectCache.get(ManagedMdt, lambda mdt: mdt.filesystem_id == target.filesystem_id)

            for mdt in mdts:
                deps.append(DependOn(mdt, "mounted"))

        return DependAll(deps)


class MountStep(AnyTargetMountStep):
    idempotent = True

    def run(self, kwargs):
        target = kwargs['target']

        result = self._run_agent_command(target, "start_target", {'ha_label': target.ha_label})
        target.update_active_mount(result['location'])


class StartTargetJob(StateChangeJob):
    stateful_object = 'target'
    state_transition = (ManagedTarget, 'unmounted', 'mounted')
    state_verb = "Start"
    target = models.ForeignKey(ManagedTarget)

    class Meta:
        app_label = 'chroma_core'
        ordering = ['id']

    def description(self):
        return "Start target %s" % self.target

    def get_deps(self):
        lnet_deps = []
        # Depend on at least one targetmount having lnet up
        mtms = ObjectCache.get(ManagedTargetMount, lambda mtm: mtm.target_id == self.target_id)
        for tm in mtms:
            lnet_deps.append(DependOn(tm.host, 'lnet_up', fix_state = 'unmounted'))
        return DependAny(lnet_deps)

    def get_steps(self):
        return [(MountStep, {"target": self.target})]


class UnmountStep(AnyTargetMountStep):
    idempotent = True

    def run(self, kwargs):
        target = kwargs['target']

        self._run_agent_command(target, "stop_target", {'ha_label': target.ha_label})
        target.active_mount = None


class StopTargetJob(StateChangeJob):
    stateful_object = 'target'
    state_transition = (ManagedTarget, 'mounted', 'unmounted')
    state_verb = "Stop"
    target = models.ForeignKey(ManagedTarget)

    def get_requires_confirmation(self):
        return True

    class Meta:
        app_label = 'chroma_core'
        ordering = ['id']

    def description(self):
        return "Stop target %s" % self.target

    def get_steps(self):
        return [(UnmountStep, {"target": self.target})]


class MkfsStep(Step):
    timeout = 3600

    def _mkfs_args(self, target):
        from chroma_core.models import ManagedMgs, ManagedMdt, ManagedOst, FilesystemMember
        kwargs = {}
        primary_mount = target.managedtargetmount_set.get(primary = True)

        kwargs['target_types'] = {
            ManagedMgs: "mgs",
            ManagedMdt: "mdt",
            ManagedOst: "ost"
            }[target.__class__]

        if isinstance(target, FilesystemMember):
            kwargs['fsname'] = target.filesystem.name
            kwargs['mgsnode'] = target.filesystem.mgs.nids()

        # FIXME: HYD-266
        kwargs['reformat'] = True

        fail_nids = target.get_failover_nids()
        if fail_nids:
            kwargs['failnode'] = fail_nids

        kwargs['device'] = primary_mount.volume_node.path
        if isinstance(target, FilesystemMember):
            kwargs['index'] = target.index

        mkfsoptions = []
        if target.inode_size:
            mkfsoptions.append("-I %s" % (target.inode_size))
        if target.bytes_per_inode:
            mkfsoptions.append("-i %s" % (target.bytes_per_inode))
        if target.inode_count:
            mkfsoptions.append("-N %s" % (target.inode_count))
        if mkfsoptions:
            kwargs['mkfsoptions'] = " ".join(mkfsoptions)

        # HYD-1089 should supercede these settings
        if isinstance(target, ManagedOst) and settings.LUSTRE_MKFS_OPTIONS_OST:
            kwargs['mkfsoptions'] = settings.LUSTRE_MKFS_OPTIONS_OST
        elif isinstance(target, ManagedMdt) and settings.LUSTRE_MKFS_OPTIONS_MDT:
            kwargs['mkfsoptions'] = settings.LUSTRE_MKFS_OPTIONS_MDT

        return kwargs

    @classmethod
    def describe(cls, kwargs):
        target = kwargs['target']
        target_mount = target.managedtargetmount_set.get(primary = True)
        return "Format %s on %s" % (target, target_mount.host)

    def run(self, kwargs):
        target = kwargs['target']
        target_mount = target.managedtargetmount_set.get(primary = True)

        args = self._mkfs_args(target)
        result = self.invoke_agent(target_mount.host, "format_target", args)
        target.uuid = result['uuid']

        # Check that inode_size was applied correctly
        if target.inode_size:
            if target.inode_size != result['inode_size']:
                raise RuntimeError("Failed for format target with inode size %s, actual inode size %s" % (
                    target.inode_size, result['inode_size']))

        # Check that inode_count was applied correctly
        if target.inode_count:
            if target.inode_count != result['inode_count']:
                raise RuntimeError("Failed for format target with inode count %s, actual inode count %s" % (
                    target.inode_count, result['inode_count']))

        # NB cannot check that bytes_per_inode was applied correctly as that setting is not stored in the FS
        target.inode_count = result['inode_count']
        target.inode_size = result['inode_size']

        target.save()


class FormatTargetJob(StateChangeJob):
    state_transition = (ManagedTarget, 'unformatted', 'formatted')
    target = models.ForeignKey(ManagedTarget)
    stateful_object = 'target'
    state_verb = 'Format'
    cancellable = False

    class Meta:
        app_label = 'chroma_core'
        ordering = ['id']

    def description(self):
        return "Format %s" % self.target

    def get_deps(self):
        from chroma_core.models import ManagedFilesystem

        ct = ContentType.objects.get_for_id(self.target.content_type_id)
        target = ObjectCache.get_one(ct.model_class(), lambda t: t.id == self.target.id)
        deps = []

        hosts = set()
        for tm in ObjectCache.get(ManagedTargetMount, lambda mtm: mtm.target_id == target.id):
            hosts.add(tm.host_id)
        for lnc in ObjectCache.get(LNetConfiguration, lambda lnc: lnc.host_id in hosts):
            deps.append(DependOn(lnc, 'nids_known'))

        if isinstance(target, FilesystemMember):
            filesystem = ObjectCache.get_one(ManagedFilesystem, lambda mf: mf.id == target.filesystem_id)
            mgt_id = filesystem.mgs_id

            mgs_hosts = set()
            for tm in ObjectCache.get(ManagedTargetMount, lambda mtm: mtm.target_id == mgt_id):
                mgs_hosts.add(tm.host_id)

            for lnc in ObjectCache.get(LNetConfiguration, lambda lnc: lnc.host_id in mgs_hosts):
                deps.append(DependOn(lnc, 'nids_known'))

        return DependAll(deps)

    def get_steps(self):
        return [(MkfsStep, {'target': self.target})]


class MigrateTargetJob(AdvertisedJob):
    target = models.ForeignKey(ManagedTarget)

    requires_confirmation = False

    classes = ['ManagedTarget']

    class Meta:
        abstract = True
        app_label = 'chroma_core'

    @classmethod
    def get_args(cls, target):
        return {'target_id': target.id}

    @classmethod
    def can_run(cls, instance):
        return False

    def create_locks(self):
        locks = super(MigrateTargetJob, self).create_locks()

        locks.append(StateLock(
            job = self,
            locked_item = self.target,
            begin_state = 'mounted',
            end_state = 'mounted',
            write = True
        ))

        return locks


class FailbackTargetStep(Step):
    idempotent = True

    def run(self, kwargs):
        target = kwargs['target']
        primary = target.primary_host
        self.invoke_agent(primary, "failback_target", {'ha_label': target.ha_label})
        target.active_mount = target.managedtargetmount_set.get(primary = True)
        target.save()


class FailbackTargetJob(MigrateTargetJob):
    verb = "Failback"

    class Meta:
        app_label = 'chroma_core'
        ordering = ['id']

    @classmethod
    def can_run(cls, instance):
        return len(instance.failover_hosts) > 0 and \
                instance.active_host is not None and\
                instance.primary_host != instance.active_host
        # HYD-1238: once we have a valid online/offline piece of info for each host,
        # reinstate the condition
        #instance.primary_host.is_available() and \

    def description(self):
        return "Migrate failed-over target back to primary host"

    def get_deps(self):
        return DependAll(
            [DependOn(self.target, 'mounted')] +
            [DependOn(self.target.active_host, 'lnet_up')] +
            [DependOn(self.target.primary_host, 'lnet_up')]
        )

    def get_steps(self):
        return [(FailbackTargetStep, {'target': self.target})]


class FailoverTargetStep(Step):
    idempotent = True

    def run(self, kwargs):
        target = kwargs['target']
        secondary = target.failover_hosts[0]
        self.invoke_agent(secondary, "failover_target", {'ha_label': target.ha_label})
        target.active_mount = target.managedtargetmount_set.get(primary = False)
        target.save()


class FailoverTargetJob(MigrateTargetJob):
    verb = "Failover"

    class Meta:
        app_label = 'chroma_core'
        ordering = ['id']

    @classmethod
    def can_run(cls, instance):
        return len(instance.failover_hosts) > 0 and\
               instance.primary_host == instance.active_host
    # HYD-1238: once we have a valid online/offline piece of info for each host,
    # reinstate the condition
#                instance.failover_hosts[0].is_available() and \

    def description(self):
        return "Migrate target to secondary host"

    def get_deps(self):
        return DependAll(
            [DependOn(self.target, 'mounted')] +
            [DependOn(self.target.primary_host, 'lnet_up')] +
            [DependOn(self.target.failover_hosts[0], 'lnet_up')]
        )

    def get_steps(self):
        return [(FailoverTargetStep, {'target': self.target})]


class ManagedTargetMount(models.Model):
    """Associate a particular Lustre target with a device node on a host"""
    __metaclass__ = DeletableMetaclass

    # FIXME: both VolumeNode and TargetMount refer to the host
    host = models.ForeignKey('ManagedHost')
    mount_point = models.CharField(max_length = 512, null = True, blank = True)
    volume_node = models.ForeignKey('VolumeNode')
    primary = models.BooleanField()
    target = models.ForeignKey('ManagedTarget')

    def save(self, force_insert = False, force_update = False, using = None):
        # If primary is true, then target must be unique
        if self.primary:
            from django.db.models import Q
            other_primaries = ManagedTargetMount.objects.filter(~Q(id = self.id), target = self.target, primary = True)
            if other_primaries.count() > 0:
                from django.core.exceptions import ValidationError
                raise ValidationError("Cannot have multiple primary mounts for target %s" % self.target)

        # If this is an MGS, there may not be another MGS on
        # this host
        if issubclass(self.target.downcast_class, ManagedMgs):
            from django.db.models import Q
            other_mgs_mountables_local = ManagedTargetMount.objects.filter(~Q(id = self.id), target__in = ManagedMgs.objects.all(), host = self.host).count()
            if other_mgs_mountables_local > 0:
                from django.core.exceptions import ValidationError
                raise ValidationError("Cannot have multiple MGS mounts on host %s" % self.host.address)

        return super(ManagedTargetMount, self).save(force_insert, force_update, using)

    def device(self):
        return self.volume_node.path

    class Meta:
        app_label = 'chroma_core'
        ordering = ['id']

    def __str__(self):
        if self.primary:
            kind_string = "primary"
        elif not self.volume_node:
            kind_string = "failover_nodev"
        else:
            kind_string = "failover"

        return "%s:%s:%s" % (self.host, kind_string, self.target)


class TargetOfflineAlert(AlertState):
    def message(self):
        return "Target %s offline" % (self.alert_item)

    class Meta:
        app_label = 'chroma_core'
        ordering = ['id']

    def begin_event(self):
        return AlertEvent(
            message_str = "%s stopped" % self.alert_item,
            host = self.alert_item.primary_server(),
            alert = self,
            severity = logging.WARNING)

    def end_event(self):
        return AlertEvent(
            message_str = "%s started" % self.alert_item,
            host = self.alert_item.primary_server(),
            alert = self,
            severity = logging.INFO)


class TargetFailoverAlert(AlertState):
    def message(self):
        return "Target %s failed over to server %s" % (self.alert_item.target, self.alert_item.host)

    class Meta:
        app_label = 'chroma_core'
        ordering = ['id']

    def begin_event(self):
        # FIXME: reporting this event against the primary server
        # of a target because we don't have enough information
        # to
        return AlertEvent(
            message_str = "%s failover mounted" % self.alert_item.target,
            host = self.alert_item.host,
            alert = self,
            severity = logging.WARNING)

    def end_event(self):
        return AlertEvent(
            message_str = "%s failover unmounted" % self.alert_item.target,
            host = self.alert_item.host,
            alert = self,
            severity = logging.INFO)


class TargetRecoveryAlert(AlertState):
    def message(self):
        return "Target %s in recovery" % self.alert_item

    class Meta:
        app_label = 'chroma_core'
        ordering = ['id']

    def begin_event(self):
        return AlertEvent(
            message_str = "Target '%s' went into recovery" % self.alert_item,
            host = self.alert_item.primary_server(),
            alert = self,
            severity = logging.WARNING)

    def end_event(self):
        return AlertEvent(
            message_str = "Target '%s' completed recovery" % self.alert_item,
            host = self.alert_item.primary_server(),
            alert = self,
            severity = logging.INFO)
