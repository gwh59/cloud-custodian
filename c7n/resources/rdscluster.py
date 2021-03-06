# Copyright 2016 Capital One Services, LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import logging

from concurrent.futures import as_completed

from c7n.actions import ActionRegistry, BaseAction
from c7n.filters import FilterRegistry, AgeFilter, OPERATORS
from c7n.manager import resources
from c7n.query import QueryResourceManager
from c7n.utils import (
    type_schema, local_session, snapshot_identifier, chunks)

log = logging.getLogger('custodian.rds-cluster')

filters = FilterRegistry('rds-cluster.filters')
actions = ActionRegistry('rds-cluster.actions')


@resources.register('rds-cluster')
class RDSCluster(QueryResourceManager):
    """Resource manager for RDS clusters.
    """

    class Meta(object):

        service = 'rds'
        type = 'rds-cluster'
        enum_spec = ('describe_db_clusters', 'DBClusters', None)
        name = id = 'DBClusterIdentifier'
        filter_name = None
        filter_type = None
        dimension = 'DBClusterIdentifier'
        date = None

    resource_type = Meta
    filter_registry = filters
    action_registry = actions


@actions.register('delete')
class Delete(BaseAction):

    schema = type_schema(
        'delete', **{'skip-snapshot': {'type': 'boolean'}})

    def process(self, resources):
        self.skip = self.data.get('skip-snapshot', False)
        client = local_session(self.manager.session_factory).client('rds')

        for cluster in resources:
            params = {'DBClusterIdentifier': cluster['DBClusterIdentifier']}
            if self.skip:
                params['SkipFinalSnapshot'] = True
            else:
                params['FinalDBSnapshotIdentifier'] = snapshot_identifier(
                    'Final', cluster['DBClusterIdentifier'])
            try:
                client.delete_db_cluster(**params)
            except ClientError as e:
                if e.response['Error']['Code'] in ['InvalidDBClusterStateFault']:
                    continue
                raise

            self.log.info('Deleted RDS cluster: %s' % cluster['DBClusterIdentifier'])


@actions.register('retention')
class RetentionWindow(BaseAction):

    date_attribute = "BackupRetentionPeriod"
    # Tag copy not yet available for Aurora:
    #   https://forums.aws.amazon.com/thread.jspa?threadID=225812
    schema = type_schema(
        'retention',
        **{'days': {'type': 'number'}})

    def process(self, resources):
        with self.executor_factory(max_workers=2) as w:
            futures = []
            for resource in resources:
                futures.append(w.submit(
                    self.process_snapshot_retention,
                    resource))
                for f in as_completed(futures):
                    if f.exception():
                        self.log.error(
                            "Exception setting RDS cluster retention  \n %s" % (
                                f.exception()))

    def process_snapshot_retention(self, resource):
        current_retention = int(resource.get('BackupRetentionPeriod', 0))
        new_retention = self.data['days']

        if current_retention < new_retention:
            self.set_retention_window(
                resource,
                max(current_retention, new_retention))
            return resource

    def set_retention_window(self, resource, retention):
        c = local_session(self.manager.session_factory).client('rds')
        c.modify_db_cluster(
            DBClusterIdentifier=resource['DBClusterIdentifier'],
            BackupRetentionPeriod=retention,
            PreferredBackupWindow=resource['PreferredBackupWindow'],
            PreferredMaintenanceWindow=resource['PreferredMaintenanceWindow'])


@actions.register('snapshot')
class Snapshot(BaseAction):

    schema = type_schema('snapshot')

    def process(self, resources):
        with self.executor_factory(max_workers=3) as w:
            futures = []
            for resource in resources:
                futures.append(w.submit(
                    self.process_cluster_snapshot,
                    resource))
                for f in as_completed(futures):
                    if f.exception():
                        self.log.error(
                            "Exception creating RDS cluster snapshot  \n %s" % (
                                f.exception()))
        return resources

    def process_cluster_snapshot(self, resource):
        c = local_session(self.manager.session_factory).client('rds')
        c.create_db_cluster_snapshot(
            DBClusterSnapshotIdentifier=snapshot_identifier(
                'Backup',
                resource['DBClusterIdentifier']),
            DBClusterIdentifier=resource['DBClusterIdentifier'])


@resources.register('rds-cluster-snapshot')
class RDSClusterSnapshot(QueryResourceManager):
    """Resource manager for RDS cluster snapshots.
    """

    class Meta(object):

        service = 'rds'
        type = 'rds-cluster-snapshot'
        enum_spec = ('describe_db_cluster_snapshots', 'DBClusterSnapshots', None)
        name = id = 'DBClusterSnapshotIdentifier'
        filter_name = None
        filter_type = None
        dimension = None
        date = 'SnapshotCreateTime'

    resource_type = Meta

    filter_registry = FilterRegistry('rdscluster-snapshot.filters')
    action_registry = ActionRegistry('rdscluster-snapshot.actions')


@RDSClusterSnapshot.filter_registry.register('age')
class RDSSnapshotAge(AgeFilter):

    schema = type_schema(
        'age', days={'type': 'number'},
        op={'type': 'string', 'enum': OPERATORS.keys()})

    date_attribute = 'SnapshotCreateTime'


@RDSClusterSnapshot.action_registry.register('delete')
class RDSClusterSnapshotDelete(BaseAction):

    def process(self, snapshots):
        log.info("Deleting %d RDS cluster snapshots", len(snapshots))
        with self.executor_factory(max_workers=3) as w:
            futures = []
            for snapshot_set in chunks(reversed(snapshots), size=50):
                futures.append(
                    w.submit(self.process_snapshot_set, snapshot_set))
                for f in as_completed(futures):
                    if f.exception():
                        self.log.error(
                            "Exception deleting snapshot set \n %s" % (
                                f.exception()))
        return snapshots

    def process_snapshot_set(self, snapshots_set):
        c = local_session(self.manager.session_factory).client('rds')
        for s in snapshots_set:
            try:
                c.delete_db_cluster_snapshot(
                    DBClusterSnapshotIdentifier=s['DBClusterSnapshotIdentifier'])
            except ClientError as e:
                raise
