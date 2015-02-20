# -*- test-case-name: twext.enterprise.dal.test.test_record -*-
##
# Copyright (c) 2015 Apple Inc. All rights reserved.
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
##

from collections import namedtuple
from pycalendar.datetime import DateTime

from twext.enterprise.dal.syntax import Insert, Parameter, Update, Delete, \
    Select, Max
from twext.python.clsprop import classproperty
from twext.python.log import Logger

from twisted.internet.defer import inlineCallbacks, returnValue, succeed

from txdav.base.propertystore.base import PropertyName
from txdav.common.datastore.sql_tables import _BIND_MODE_OWN, _BIND_MODE_DIRECT, \
    _BIND_MODE_INDIRECT, _BIND_STATUS_ACCEPTED, _BIND_STATUS_DECLINED, \
    _BIND_STATUS_INVITED, _BIND_STATUS_INVALID, _BIND_STATUS_DELETED
from txdav.common.icommondatastore import ExternalShareFailed, \
    HomeChildNameAlreadyExistsError, AllRetriesFailed
from txdav.xml import element

from uuid import uuid4


log = Logger()

"""
Classes and methods that relate to sharing in the SQL store.
"""

class SharingHomeMixIn(object):
    """
    Common class for CommonHome to implement sharing operations
    """

    @inlineCallbacks
    def acceptShare(self, shareUID, summary=None):
        """
        This share is being accepted.
        """

        shareeView = yield self.anyObjectWithShareUID(shareUID)
        if shareeView is not None:
            yield shareeView.acceptShare(summary)

        returnValue(shareeView)


    @inlineCallbacks
    def declineShare(self, shareUID):
        """
        This share is being declined.
        """

        shareeView = yield self.anyObjectWithShareUID(shareUID)
        if shareeView is not None:
            yield shareeView.declineShare()

        returnValue(shareeView is not None)


    #
    # External (cross-pod) sharing - entry point is the sharee's home collection.
    #
    @inlineCallbacks
    def processExternalInvite(
        self, ownerUID, ownerRID, ownerName, shareUID, bindMode, summary,
        copy_invite_properties, supported_components=None
    ):
        """
        External invite received.
        """

        # Get the owner home - create external one if not present
        ownerHome = yield self._txn.homeWithUID(
            self._homeType, ownerUID, create=True
        )
        if ownerHome is None or not ownerHome.external():
            raise ExternalShareFailed("Invalid owner UID: {}".format(ownerUID))

        # Try to find owner calendar via its external id
        ownerView = yield ownerHome.childWithExternalID(ownerRID)
        if ownerView is None:
            try:
                ownerView = yield ownerHome.createChildWithName(
                    ownerName, externalID=ownerRID
                )
            except HomeChildNameAlreadyExistsError:
                # This is odd - it means we possibly have a left over sharer
                # collection which the sharer likely removed and re-created
                # with the same name but now it has a different externalID and
                # is not found by the initial query. What we do is check to see
                # whether any shares still reference the old ID - if they do we
                # are hosed. If not, we can remove the old item and create a new one.
                oldOwnerView = yield ownerHome.childWithName(ownerName)
                invites = yield oldOwnerView.sharingInvites()
                if len(invites) != 0:
                    log.error(
                        "External invite collection name is present with a "
                        "different externalID and still has shares"
                    )
                    raise
                log.error(
                    "External invite collection name is present with a "
                    "different externalID - trying to fix"
                )
                yield ownerHome.removeExternalChild(oldOwnerView)
                ownerView = yield ownerHome.createChildWithName(
                    ownerName, externalID=ownerRID
                )

            if (
                supported_components is not None and
                hasattr(ownerView, "setSupportedComponents")
            ):
                yield ownerView.setSupportedComponents(supported_components)

        # Now carry out the share operation
        if bindMode == _BIND_MODE_DIRECT:
            shareeView = yield ownerView.directShareWithUser(
                self.uid(), shareName=shareUID
            )
        else:
            shareeView = yield ownerView.inviteUIDToShare(
                self.uid(), bindMode, summary, shareName=shareUID
            )

        shareeView.setInviteCopyProperties(copy_invite_properties)


    @inlineCallbacks
    def processExternalUninvite(self, ownerUID, ownerRID, shareUID):
        """
        External invite received.
        """

        # Get the owner home
        ownerHome = yield self._txn.homeWithUID(self._homeType, ownerUID)
        if ownerHome is None or not ownerHome.external():
            raise ExternalShareFailed("Invalid owner UID: {}".format(ownerUID))

        # Try to find owner calendar via its external id
        ownerView = yield ownerHome.childWithExternalID(ownerRID)
        if ownerView is None:
            raise ExternalShareFailed("Invalid share ID: {}".format(shareUID))

        # Now carry out the share operation
        yield ownerView.uninviteUIDFromShare(self.uid())

        # See if there are any references to the external share. If not,
        # remove it
        invites = yield ownerView.sharingInvites()
        if len(invites) == 0:
            yield ownerHome.removeExternalChild(ownerView)


    @inlineCallbacks
    def processExternalReply(
        self, ownerUID, shareeUID, shareUID, bindStatus, summary=None
    ):
        """
        External invite received.
        """

        # Make sure the shareeUID and shareUID match

        # Get the owner home - create external one if not present
        shareeHome = yield self._txn.homeWithUID(self._homeType, shareeUID)
        if shareeHome is None or not shareeHome.external():
            raise ExternalShareFailed(
                "Invalid sharee UID: {}".format(shareeUID)
            )

        # Try to find owner calendar via its external id
        shareeView = yield shareeHome.anyObjectWithShareUID(shareUID)
        if shareeView is None:
            raise ExternalShareFailed("Invalid share UID: {}".format(shareUID))

        # Now carry out the share operation
        if bindStatus == _BIND_STATUS_ACCEPTED:
            yield shareeHome.acceptShare(shareUID, summary)
        elif bindStatus == _BIND_STATUS_DECLINED:
            if shareeView.direct():
                yield shareeView.deleteShare()
            else:
                yield shareeHome.declineShare(shareUID)



SharingInvitation = namedtuple(
    "SharingInvitation",
    ["uid", "ownerUID", "ownerHomeID", "shareeUID", "shareeHomeID", "mode", "status", "summary"]
)



class SharingMixIn(object):
    """
    Common class for CommonHomeChild and AddressBookObject
    """

    @classproperty
    def _bindInsertQuery(cls, **kw):
        """
        DAL statement to create a bind entry that connects a collection to its
        home.
        """
        bind = cls._bindSchema
        return Insert({
            bind.HOME_RESOURCE_ID: Parameter("homeID"),
            bind.RESOURCE_ID: Parameter("resourceID"),
            bind.EXTERNAL_ID: Parameter("externalID"),
            bind.RESOURCE_NAME: Parameter("name"),
            bind.BIND_MODE: Parameter("mode"),
            bind.BIND_STATUS: Parameter("bindStatus"),
            bind.MESSAGE: Parameter("message"),
        })


    @classmethod
    def _updateBindColumnsQuery(cls, columnMap):
        bind = cls._bindSchema
        return Update(
            columnMap,
            Where=(bind.RESOURCE_ID == Parameter("resourceID")).And(
                bind.HOME_RESOURCE_ID == Parameter("homeID")),
        )


    @classproperty
    def _deleteBindForResourceIDAndHomeID(cls):
        bind = cls._bindSchema
        return Delete(
            From=bind,
            Where=(bind.RESOURCE_ID == Parameter("resourceID")).And(
                bind.HOME_RESOURCE_ID == Parameter("homeID")),
        )


    @classmethod
    def _bindFor(cls, condition):
        bind = cls._bindSchema
        columns = cls.bindColumns() + cls.additionalBindColumns()
        return Select(
            columns,
            From=bind,
            Where=condition
        )


    @classmethod
    def _bindInviteFor(cls, condition):
        home = cls._homeSchema
        bind = cls._bindSchema
        return Select(
            [
                home.OWNER_UID,
                bind.HOME_RESOURCE_ID,
                bind.RESOURCE_ID,
                bind.RESOURCE_NAME,
                bind.BIND_MODE,
                bind.BIND_STATUS,
                bind.MESSAGE,
            ],
            From=bind.join(home, on=(bind.HOME_RESOURCE_ID == home.RESOURCE_ID)),
            Where=condition
        )


    @classproperty
    def _sharedInvitationBindForResourceID(cls):
        bind = cls._bindSchema
        return cls._bindInviteFor(
            (bind.RESOURCE_ID == Parameter("resourceID")).And
            (bind.BIND_MODE != _BIND_MODE_OWN)
        )


    @classproperty
    def _acceptedBindForHomeID(cls):
        bind = cls._bindSchema
        return cls._bindFor((bind.HOME_RESOURCE_ID == Parameter("homeID"))
                            .And(bind.BIND_STATUS == _BIND_STATUS_ACCEPTED))


    @classproperty
    def _bindForResourceIDAndHomeID(cls):
        """
        DAL query that looks up home bind rows by home child
        resource ID and home resource ID.
        """
        bind = cls._bindSchema
        return cls._bindFor((bind.RESOURCE_ID == Parameter("resourceID"))
                            .And(bind.HOME_RESOURCE_ID == Parameter("homeID")))


    @classproperty
    def _bindForExternalIDAndHomeID(cls):
        """
        DAL query that looks up home bind rows by home child
        resource ID and home resource ID.
        """
        bind = cls._bindSchema
        return cls._bindFor((bind.EXTERNAL_ID == Parameter("externalID"))
                            .And(bind.HOME_RESOURCE_ID == Parameter("homeID")))


    @classproperty
    def _bindForNameAndHomeID(cls):
        """
        DAL query that looks up any bind rows by home child
        resource ID and home resource ID.
        """
        bind = cls._bindSchema
        return cls._bindFor((bind.RESOURCE_NAME == Parameter("name"))
                            .And(bind.HOME_RESOURCE_ID == Parameter("homeID")))


    #
    # Higher level API
    #
    @inlineCallbacks
    def inviteUIDToShare(self, shareeUID, mode, summary=None, shareName=None):
        """
        Invite a user to share this collection - either create the share if it does not exist, or
        update the existing share with new values. Make sure a notification is sent as well.

        @param shareeUID: UID of the sharee
        @type shareeUID: C{str}
        @param mode: access mode
        @type mode: C{int}
        @param summary: share message
        @type summary: C{str}
        """

        # Look for existing invite and update its fields or create new one
        shareeView = yield self.shareeView(shareeUID)
        if shareeView is not None:
            status = _BIND_STATUS_INVITED if shareeView.shareStatus() in (_BIND_STATUS_DECLINED, _BIND_STATUS_INVALID) else None
            yield self.updateShare(shareeView, mode=mode, status=status, summary=summary)
        else:
            shareeView = yield self.createShare(shareeUID=shareeUID, mode=mode, summary=summary, shareName=shareName)

        # Check for external
        if shareeView.viewerHome().external():
            yield self._sendExternalInvite(shareeView)
        else:
            # Send invite notification
            yield self._sendInviteNotification(shareeView)
        returnValue(shareeView)


    @inlineCallbacks
    def directShareWithUser(self, shareeUID, shareName=None):
        """
        Create a direct share with the specified user. Note it is currently up to the app layer
        to enforce access control - this is not ideal as we really should have control of that in
        the store. Once we do, this api will need to verify that access is allowed for a direct share.

        NB no invitations are used with direct sharing.

        @param shareeUID: UID of the sharee
        @type shareeUID: C{str}
        """

        # Ignore if it already exists
        shareeView = yield self.shareeView(shareeUID)
        if shareeView is None:
            shareeView = yield self.createShare(shareeUID=shareeUID, mode=_BIND_MODE_DIRECT, shareName=shareName)
            yield shareeView.newShare()

            # Check for external
            if shareeView.viewerHome().external():
                yield self._sendExternalInvite(shareeView)

        returnValue(shareeView)


    @inlineCallbacks
    def uninviteUIDFromShare(self, shareeUID):
        """
        Remove a user from a share. Make sure a notification is sent as well.

        @param shareeUID: UID of the sharee
        @type shareeUID: C{str}
        """
        # Cancel invites - we'll just use whatever userid we are given

        shareeView = yield self.shareeView(shareeUID)
        if shareeView is not None:
            if shareeView.viewerHome().external():
                yield self._sendExternalUninvite(shareeView)
            else:
                # If current user state is accepted then we send an invite with the new state, otherwise
                # we cancel any existing invites for the user
                if not shareeView.direct():
                    if shareeView.shareStatus() != _BIND_STATUS_ACCEPTED:
                        yield self._removeInviteNotification(shareeView)
                    else:
                        yield self._sendInviteNotification(shareeView, notificationState=_BIND_STATUS_DELETED)

            # Remove the bind
            yield self.removeShare(shareeView)


    @inlineCallbacks
    def acceptShare(self, summary=None):
        """
        This share is being accepted.
        """

        if not self.direct() and self.shareStatus() != _BIND_STATUS_ACCEPTED:
            if self.external():
                yield self._replyExternalInvite(_BIND_STATUS_ACCEPTED, summary)
            ownerView = yield self.ownerView()
            yield ownerView.updateShare(self, status=_BIND_STATUS_ACCEPTED)
            yield self.newShare(displayname=summary)
            if not ownerView.external():
                yield self._sendReplyNotification(ownerView, summary)


    @inlineCallbacks
    def declineShare(self):
        """
        This share is being declined.
        """

        if not self.direct() and self.shareStatus() != _BIND_STATUS_DECLINED:
            if self.external():
                yield self._replyExternalInvite(_BIND_STATUS_DECLINED)
            ownerView = yield self.ownerView()
            yield ownerView.updateShare(self, status=_BIND_STATUS_DECLINED)
            if not ownerView.external():
                yield self._sendReplyNotification(ownerView)


    @inlineCallbacks
    def deleteShare(self):
        """
        This share is being deleted (by the sharee) - either decline or remove (for direct shares).
        """

        ownerView = yield self.ownerView()
        if self.direct():
            yield ownerView.removeShare(self)
            if ownerView.external():
                yield self._replyExternalInvite(_BIND_STATUS_DECLINED)
        else:
            yield self.declineShare()


    @inlineCallbacks
    def ownerDeleteShare(self):
        """
        This share is being deleted (by the owner) - either decline or remove (for direct shares).
        """

        # Change status on store object
        yield self.setShared(False)

        # Remove all sharees (direct and invited)
        for invitation in (yield self.sharingInvites()):
            yield self.uninviteUIDFromShare(invitation.shareeUID)


    def newShare(self, displayname=None):
        """
        Override in derived classes to do any specific operations needed when a share
        is first accepted.
        """
        return succeed(None)


    @inlineCallbacks
    def allInvitations(self):
        """
        Get list of all invitations (non-direct) to this object.
        """
        invitations = yield self.sharingInvites()

        # remove direct shares as those are not "real" invitations
        invitations = filter(lambda x: x.mode != _BIND_MODE_DIRECT, invitations)
        invitations.sort(key=lambda invitation: invitation.shareeUID)
        returnValue(invitations)


    @inlineCallbacks
    def _sendInviteNotification(self, shareeView, notificationState=None):
        """
        Called on the owner's resource.
        """
        # When deleting the message is the sharee's display name
        displayname = shareeView.shareMessage()
        if notificationState == _BIND_STATUS_DELETED:
            displayname = str(shareeView.properties().get(PropertyName.fromElement(element.DisplayName), displayname))

        notificationtype = {
            "notification-type": "invite-notification",
            "shared-type": shareeView.sharedResourceType(),
        }
        notificationdata = {
            "notification-type": "invite-notification",
            "shared-type": shareeView.sharedResourceType(),
            "dtstamp": DateTime.getNowUTC().getText(),
            "owner": shareeView.ownerHome().uid(),
            "sharee": shareeView.viewerHome().uid(),
            "uid": shareeView.shareUID(),
            "status": shareeView.shareStatus() if notificationState is None else notificationState,
            "access": (yield shareeView.effectiveShareMode()),
            "ownerName": self.shareName(),
            "summary": displayname,
        }
        if hasattr(self, "getSupportedComponents"):
            notificationdata["supported-components"] = self.getSupportedComponents()

        # Add to sharee's collection
        notifications = yield self._txn.notificationsWithUID(shareeView.viewerHome().uid())
        yield notifications.writeNotificationObject(shareeView.shareUID(), notificationtype, notificationdata)


    @inlineCallbacks
    def _sendReplyNotification(self, ownerView, summary=None):
        """
        Create a reply notification based on the current state of this shared resource.
        """

        # Generate invite XML
        notificationUID = "%s-reply" % (self.shareUID(),)

        notificationtype = {
            "notification-type": "invite-reply",
            "shared-type": self.sharedResourceType(),
        }

        notificationdata = {
            "notification-type": "invite-reply",
            "shared-type": self.sharedResourceType(),
            "dtstamp": DateTime.getNowUTC().getText(),
            "owner": self.ownerHome().uid(),
            "sharee": self.viewerHome().uid(),
            "status": self.shareStatus(),
            "ownerName": ownerView.shareName(),
            "in-reply-to": self.shareUID(),
            "summary": summary,
        }

        # Add to owner notification collection
        notifications = yield self._txn.notificationsWithUID(self.ownerHome().uid())
        yield notifications.writeNotificationObject(notificationUID, notificationtype, notificationdata)


    @inlineCallbacks
    def _removeInviteNotification(self, shareeView):
        """
        Called on the owner's resource.
        """

        # Remove from sharee's collection
        notifications = yield self._txn.notificationsWithUID(shareeView.viewerHome().uid())
        yield notifications.removeNotificationObjectWithUID(shareeView.shareUID())


    #
    # External/cross-pod API
    #
    @inlineCallbacks
    def _sendExternalInvite(self, shareeView):

        yield self._txn.store().conduit.send_shareinvite(
            self._txn,
            shareeView.ownerHome()._homeType,
            shareeView.ownerHome().uid(),
            self.id(),
            self.shareName(),
            shareeView.viewerHome().uid(),
            shareeView.shareUID(),
            shareeView.shareMode(),
            shareeView.shareMessage(),
            self.getInviteCopyProperties(),
            supported_components=self.getSupportedComponents() if hasattr(self, "getSupportedComponents") else None,
        )


    @inlineCallbacks
    def _sendExternalUninvite(self, shareeView):

        yield self._txn.store().conduit.send_shareuninvite(
            self._txn,
            shareeView.ownerHome()._homeType,
            shareeView.ownerHome().uid(),
            self.id(),
            shareeView.viewerHome().uid(),
            shareeView.shareUID(),
        )


    @inlineCallbacks
    def _replyExternalInvite(self, status, summary=None):

        yield self._txn.store().conduit.send_sharereply(
            self._txn,
            self.viewerHome()._homeType,
            self.ownerHome().uid(),
            self.viewerHome().uid(),
            self.shareUID(),
            status,
            summary,
        )


    #
    # Lower level API
    #
    @inlineCallbacks
    def ownerView(self):
        """
        Return the owner resource counterpart of this shared resource.

        Note we have to play a trick with the property store to coerce it to match
        the per-user properties for the owner.
        """
        # Get the child of the owner home that has the same resource id as the owned one
        ownerView = yield self.ownerHome().childWithID(self.id())
        returnValue(ownerView)


    @inlineCallbacks
    def shareeView(self, shareeUID):
        """
        Return the shared resource counterpart of this owned resource for the specified sharee.

        Note we have to play a trick with the property store to coerce it to match
        the per-user properties for the sharee.
        """

        # Never return the owner's own resource
        if self._home.uid() == shareeUID:
            returnValue(None)

        # Get the child of the sharee home that has the same resource id as the owned one
        shareeHome = yield self._txn.homeWithUID(self._home._homeType, shareeUID, authzUID=shareeUID)
        shareeView = (yield shareeHome.allChildWithID(self.id())) if shareeHome is not None else None
        returnValue(shareeView)


    @inlineCallbacks
    def shareWithUID(self, shareeUID, mode, status=None, summary=None, shareName=None):
        """
        Share this (owned) L{CommonHomeChild} with another principal.

        @param shareeUID: The UID of the sharee.
        @type: L{str}

        @param mode: The sharing mode; L{_BIND_MODE_READ} or
            L{_BIND_MODE_WRITE} or L{_BIND_MODE_DIRECT}
        @type mode: L{str}

        @param status: The sharing status; L{_BIND_STATUS_INVITED} or
            L{_BIND_STATUS_ACCEPTED}
        @type: L{str}

        @param summary: The proposed message to go along with the share, which
            will be used as the default display name.
        @type: L{str}

        @return: the name of the shared calendar in the new calendar home.
        @rtype: L{str}
        """
        shareeHome = yield self._txn.calendarHomeWithUID(shareeUID, create=True)
        returnValue(
            (yield self.shareWith(shareeHome, mode, status, summary, shareName))
        )


    @inlineCallbacks
    def shareWith(self, shareeHome, mode, status=None, summary=None, shareName=None):
        """
        Share this (owned) L{CommonHomeChild} with another home.

        @param shareeHome: The home of the sharee.
        @type: L{CommonHome}

        @param mode: The sharing mode; L{_BIND_MODE_READ} or
            L{_BIND_MODE_WRITE} or L{_BIND_MODE_DIRECT}
        @type: L{str}

        @param status: The sharing status; L{_BIND_STATUS_INVITED} or
            L{_BIND_STATUS_ACCEPTED}
        @type: L{str}

        @param summary: The proposed message to go along with the share, which
            will be used as the default display name.
        @type: L{str}

        @param shareName: The proposed name of the new share.
        @type: L{str}

        @return: the name of the shared calendar in the new calendar home.
        @rtype: L{str}
        """

        if status is None:
            status = _BIND_STATUS_ACCEPTED

        @inlineCallbacks
        def doInsert(subt):
            newName = shareName if shareName is not None else self.newShareName()
            yield self._bindInsertQuery.on(
                subt,
                homeID=shareeHome._resourceID,
                resourceID=self._resourceID,
                externalID=self._externalID,
                name=newName,
                mode=mode,
                bindStatus=status,
                message=summary
            )
            returnValue(newName)
        try:
            bindName = yield self._txn.subtransaction(doInsert)
        except AllRetriesFailed:
            # FIXME: catch more specific exception
            child = yield shareeHome.allChildWithID(self._resourceID)
            yield self.updateShare(
                child, mode=mode, status=status,
                summary=summary
            )
            bindName = child._name
        else:
            if status == _BIND_STATUS_ACCEPTED:
                shareeView = yield shareeHome.anyObjectWithShareUID(bindName)
                yield shareeView._initSyncToken()
                yield shareeView._initBindRevision()

        # Mark this as shared
        yield self.setShared(True)

        # Must send notification to ensure cache invalidation occurs
        yield self.notifyPropertyChanged()
        yield shareeHome.notifyChanged()

        returnValue(bindName)


    @inlineCallbacks
    def createShare(self, shareeUID, mode, summary=None, shareName=None):
        """
        Create a new shared resource. If the mode is direct, the share is created in accepted state,
        otherwise the share is created in invited state.
        """
        shareeHome = yield self._txn.homeWithUID(self.ownerHome()._homeType, shareeUID, create=True)

        yield self.shareWith(
            shareeHome,
            mode=mode,
            status=_BIND_STATUS_INVITED if mode != _BIND_MODE_DIRECT else _BIND_STATUS_ACCEPTED,
            summary=summary,
            shareName=shareName,
        )
        shareeView = yield self.shareeView(shareeUID)
        returnValue(shareeView)


    @inlineCallbacks
    def updateShare(self, shareeView, mode=None, status=None, summary=None):
        """
        Update share mode, status, and message for a home child shared with
        this (owned) L{CommonHomeChild}.

        @param shareeView: The sharee home child that shares this.
        @type shareeView: L{CommonHomeChild}

        @param mode: The sharing mode; L{_BIND_MODE_READ} or
            L{_BIND_MODE_WRITE} or None to not update
        @type mode: L{str}

        @param status: The sharing status; L{_BIND_STATUS_INVITED} or
            L{_BIND_STATUS_ACCEPTED} or L{_BIND_STATUS_DECLINED} or
            L{_BIND_STATUS_INVALID}  or None to not update
        @type status: L{str}

        @param summary: The proposed message to go along with the share, which
            will be used as the default display name, or None to not update
        @type summary: L{str}
        """
        # TODO: raise a nice exception if shareeView is not, in fact, a shared
        # version of this same L{CommonHomeChild}

        # remove None parameters, and substitute None for empty string
        bind = self._bindSchema
        columnMap = {}
        if mode != None and mode != shareeView._bindMode:
            columnMap[bind.BIND_MODE] = mode
        if status != None and status != shareeView._bindStatus:
            columnMap[bind.BIND_STATUS] = status
        if summary != None and summary != shareeView._bindMessage:
            columnMap[bind.MESSAGE] = summary

        if columnMap:

            # Count accepted
            if bind.BIND_STATUS in columnMap:
                previouslyAcceptedCount = yield shareeView._previousAcceptCount()

            yield self._updateBindColumnsQuery(columnMap).on(
                self._txn,
                resourceID=self._resourceID, homeID=shareeView._home._resourceID
            )

            # Update affected attributes
            if bind.BIND_MODE in columnMap:
                shareeView._bindMode = columnMap[bind.BIND_MODE]

            if bind.BIND_STATUS in columnMap:
                shareeView._bindStatus = columnMap[bind.BIND_STATUS]
                yield shareeView._changedStatus(previouslyAcceptedCount)

            if bind.MESSAGE in columnMap:
                shareeView._bindMessage = columnMap[bind.MESSAGE]

            yield shareeView.invalidateQueryCache()

            # Must send notification to ensure cache invalidation occurs
            yield self.notifyPropertyChanged()
            yield shareeView.viewerHome().notifyChanged()


    def _previousAcceptCount(self):
        return succeed(1)


    @inlineCallbacks
    def _changedStatus(self, previouslyAcceptedCount):
        if self._bindStatus == _BIND_STATUS_ACCEPTED:
            yield self._initSyncToken()
            yield self._initBindRevision()
            self._home._children[self._name] = self
            self._home._children[self._resourceID] = self
        elif self._bindStatus in (_BIND_STATUS_INVITED, _BIND_STATUS_DECLINED):
            yield self._deletedSyncToken(sharedRemoval=True)
            self._home._children.pop(self._name, None)
            self._home._children.pop(self._resourceID, None)


    @inlineCallbacks
    def removeShare(self, shareeView):
        """
        Remove the shared version of this (owned) L{CommonHomeChild} from the
        referenced L{CommonHome}.

        @see: L{CommonHomeChild.shareWith}

        @param shareeView: The shared resource being removed.

        @return: a L{Deferred} which will fire with the previous shareUID
        """

        # remove sync tokens
        shareeHome = shareeView.viewerHome()
        yield shareeView._deletedSyncToken(sharedRemoval=True)
        shareeHome._children.pop(shareeView._name, None)
        shareeHome._children.pop(shareeView._resourceID, None)

        # Must send notification to ensure cache invalidation occurs
        yield self.notifyPropertyChanged()
        yield shareeHome.notifyChanged()

        # delete binds including invites
        yield self._deleteBindForResourceIDAndHomeID.on(
            self._txn,
            resourceID=self._resourceID,
            homeID=shareeHome._resourceID,
        )

        yield shareeView.invalidateQueryCache()


    @inlineCallbacks
    def unshare(self):
        """
        Unshares a collection, regardless of which "direction" it was shared.
        """
        if self.owned():
            # This collection may be shared to others
            invites = yield self.sharingInvites()
            for invite in invites:
                shareeView = yield self.shareeView(invite.shareeUID)
                yield self.removeShare(shareeView)
        else:
            # This collection is shared to me
            ownerView = yield self.ownerView()
            yield ownerView.removeShare(self)


    @inlineCallbacks
    def sharingInvites(self):
        """
        Retrieve the list of all L{SharingInvitation}'s for this L{CommonHomeChild}, irrespective of mode.

        @return: L{SharingInvitation} objects
        @rtype: a L{Deferred} which fires with a L{list} of L{SharingInvitation}s.
        """
        if not self.owned():
            returnValue([])

        # get all accepted binds
        invitedRows = yield self._sharedInvitationBindForResourceID.on(
            self._txn, resourceID=self._resourceID, homeID=self._home._resourceID
        )

        result = []
        for homeUID, homeRID, _ignore_resourceID, resourceName, bindMode, bindStatus, bindMessage in invitedRows:
            invite = SharingInvitation(
                resourceName,
                self.ownerHome().name(),
                self.ownerHome().id(),
                homeUID,
                homeRID,
                bindMode,
                bindStatus,
                bindMessage,
            )
            result.append(invite)
        returnValue(result)


    @inlineCallbacks
    def _initBindRevision(self):
        yield self.syncToken() # init self._syncTokenRevision if None
        self._bindRevision = self._syncTokenRevision

        bind = self._bindSchema
        yield self._updateBindColumnsQuery(
            {bind.BIND_REVISION : Parameter("revision"), }
        ).on(
            self._txn,
            revision=self._bindRevision,
            resourceID=self._resourceID,
            homeID=self.viewerHome()._resourceID,
        )
        yield self.invalidateQueryCache()


    def sharedResourceType(self):
        """
        The sharing resource type. Needs to be overridden by each type of resource that can be shared.

        @return: an identifier for the type of the share.
        @rtype: C{str}
        """
        return ""


    def newShareName(self):
        """
        Name used when creating a new share. By default this is a UUID.
        """
        return str(uuid4())


    def owned(self):
        """
        @see: L{ICalendar.owned}
        """
        return self._bindMode == _BIND_MODE_OWN


    def isShared(self):
        """
        For an owned collection indicate whether it is shared.

        @return: C{True} if shared, C{False} otherwise
        @rtype: C{bool}
        """
        return self.owned() and self._bindMessage == "shared"


    @inlineCallbacks
    def setShared(self, shared):
        """
        Set an owned collection to shared or unshared state. Technically this is not useful as "shared"
        really means it has invitees, but the current sharing spec supports a notion of a shared collection
        that has not yet had invitees added. For the time being we will support that option by using a new
        MESSAGE value to indicate an owned collection that is "shared".

        @param shared: whether or not the owned collection is "shared"
        @type shared: C{bool}
        """
        assert self.owned(), "Cannot change share mode on a shared collection"

        # Only if change is needed
        newMessage = "shared" if shared else None
        if self._bindMessage == newMessage:
            returnValue(None)

        self._bindMessage = newMessage

        bind = self._bindSchema
        yield Update(
            {bind.MESSAGE: self._bindMessage},
            Where=(bind.RESOURCE_ID == Parameter("resourceID")).And(
                bind.HOME_RESOURCE_ID == Parameter("homeID")),
        ).on(self._txn, resourceID=self._resourceID, homeID=self.viewerHome()._resourceID)

        yield self.invalidateQueryCache()
        yield self.notifyPropertyChanged()


    def direct(self):
        """
        Is this a "direct" share?

        @return: a boolean indicating whether it's direct.
        """
        return self._bindMode == _BIND_MODE_DIRECT


    def indirect(self):
        """
        Is this an "indirect" share?

        @return: a boolean indicating whether it's indirect.
        """
        return self._bindMode == _BIND_MODE_INDIRECT


    def shareUID(self):
        """
        @see: L{ICalendar.shareUID}
        """
        return self.name()


    def shareMode(self):
        """
        @see: L{ICalendar.shareMode}
        """
        return self._bindMode


    def _effectiveShareMode(self, bindMode, viewerUID, txn):
        """
        Get the effective share mode without a calendar object
        """
        return bindMode


    def effectiveShareMode(self):
        """
        @see: L{ICalendar.shareMode}
        """
        return self._bindMode


    def shareName(self):
        """
        This is a path like name for the resource within the home being shared. For object resource
        shares this will be a combination of the L{CommonHomeChild} name and the L{CommonObjecrResource}
        name. Otherwise it is just the L{CommonHomeChild} name. This is needed to expose a value to the
        app-layer such that it can construct a URI for the actual WebDAV resource being shared.
        """
        name = self.name()
        if self.sharedResourceType() == "group":
            name = self.parentCollection().name() + "/" + name
        return name


    def shareStatus(self):
        """
        @see: L{ICalendar.shareStatus}
        """
        return self._bindStatus


    def accepted(self):
        """
        @see: L{ICalendar.shareStatus}
        """
        return self._bindStatus == _BIND_STATUS_ACCEPTED


    def shareMessage(self):
        """
        @see: L{ICalendar.shareMessage}
        """
        return self._bindMessage


    def getInviteCopyProperties(self):
        """
        Get a dictionary of property name/values (as strings) for properties that are shadowable and
        need to be copied to a sharee's collection when an external (cross-pod) share is created.
        Sub-classes should override to expose the properties they care about.
        """
        return {}


    def setInviteCopyProperties(self, props):
        """
        Copy a set of shadowable properties (as name/value strings) onto this shared resource when
        a cross-pod invite is processed. Sub-classes should override to expose the properties they
        care about.
        """
        pass


    @classmethod
    def metadataColumns(cls):
        """
        Return a list of column name for retrieval of metadata. This allows
        different child classes to have their own type specific data, but still make use of the
        common base logic.
        """

        # Common behavior is to have created and modified

        return (
            cls._homeChildMetaDataSchema.CREATED,
            cls._homeChildMetaDataSchema.MODIFIED,
        )


    @classmethod
    def metadataAttributes(cls):
        """
        Return a list of attribute names for retrieval of metadata. This allows
        different child classes to have their own type specific data, but still make use of the
        common base logic.
        """

        # Common behavior is to have created and modified

        return (
            "_created",
            "_modified",
        )


    @classmethod
    def bindColumns(cls):
        """
        Return a list of column names for retrieval during creation. This allows
        different child classes to have their own type specific data, but still make use of the
        common base logic.
        """

        return (
            cls._bindSchema.BIND_MODE,
            cls._bindSchema.HOME_RESOURCE_ID,
            cls._bindSchema.RESOURCE_ID,
            cls._bindSchema.EXTERNAL_ID,
            cls._bindSchema.RESOURCE_NAME,
            cls._bindSchema.BIND_STATUS,
            cls._bindSchema.BIND_REVISION,
            cls._bindSchema.MESSAGE
        )


    @classmethod
    def bindAttributes(cls):
        """
        Return a list of column names for retrieval during creation. This allows
        different child classes to have their own type specific data, but still make use of the
        common base logic.
        """

        return (
            "_bindMode",
            "_homeResourceID",
            "_resourceID",
            "_externalID",
            "_name",
            "_bindStatus",
            "_bindRevision",
            "_bindMessage",
        )

    bindColumnCount = 8

    @classmethod
    def additionalBindColumns(cls):
        """
        Return a list of column names for retrieval during creation. This allows
        different child classes to have their own type specific data, but still make use of the
        common base logic.
        """

        return ()


    @classmethod
    def additionalBindAttributes(cls):
        """
        Return a list of attribute names for retrieval of during creation. This allows
        different child classes to have their own type specific data, but still make use of the
        common base logic.
        """

        return ()


    @classproperty
    def _childrenAndMetadataForHomeID(cls):
        bind = cls._bindSchema
        child = cls._homeChildSchema
        childMetaData = cls._homeChildMetaDataSchema

        columns = cls.bindColumns() + cls.additionalBindColumns() + cls.metadataColumns()
        return Select(
            columns,
            From=child.join(
                bind, child.RESOURCE_ID == bind.RESOURCE_ID,
                'left outer').join(
                    childMetaData, childMetaData.RESOURCE_ID == bind.RESOURCE_ID,
                    'left outer'),
            Where=(bind.HOME_RESOURCE_ID == Parameter("homeID")).And(
                bind.BIND_STATUS == _BIND_STATUS_ACCEPTED)
        )


    @classmethod
    def _revisionsForResourceIDs(cls, resourceIDs):
        rev = cls._revisionsSchema
        return Select(
            [rev.RESOURCE_ID, Max(rev.REVISION)],
            From=rev,
            Where=rev.RESOURCE_ID.In(Parameter("resourceIDs", len(resourceIDs))).And(
                (rev.RESOURCE_NAME != None).Or(rev.DELETED == False)),
            GroupBy=rev.RESOURCE_ID
        )


    @inlineCallbacks
    def invalidateQueryCache(self):
        queryCacher = self._txn._queryCacher
        if queryCacher is not None:
            yield queryCacher.invalidateAfterCommit(self._txn, queryCacher.keyForHomeChildMetaData(self._resourceID))
            yield queryCacher.invalidateAfterCommit(self._txn, queryCacher.keyForObjectWithName(self._home._resourceID, self._name))
            yield queryCacher.invalidateAfterCommit(self._txn, queryCacher.keyForObjectWithResourceID(self._home._resourceID, self._resourceID))
            yield queryCacher.invalidateAfterCommit(self._txn, queryCacher.keyForObjectWithExternalID(self._home._resourceID, self._externalID))