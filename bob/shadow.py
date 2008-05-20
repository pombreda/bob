#
# Copyright (c) 2008 rPath, Inc.
#
# All rights reserved.
#

'''
Tools for manipulating recipes and source troves.
'''

import logging
import os
import shutil
import tempfile

from conary.build import cook
from conary.build import use
from conary.build.loadrecipe import RecipeLoader
from conary.build.lookaside import RepositoryCache
from conary.build.recipe import isPackageRecipe
from conary.changelog import ChangeLog
from conary.conaryclient import filetypes
from conary.deps import deps
from conary.files import ThawFile
from conary.lib.sha1helper import sha1String
from conary.repository.changeset import ChangedFileTypes, ChangeSet
from conary.trove import Trove
from conary.versions import Revision
from rmake import compat

from bob import autosource
from bob.mangle import mangle
from bob.util import findFile

log = logging.getLogger('bob.shadow')


class ShadowBatch(object):
    def __init__(self):
        self._packages = {}

        self._helper = self._mangleData = None
        self._packageList = None

        self._upstreamChangeSet = None
        self._upstreamTroveCSets = None

        self._recipes = None
        self._recipeObjs = None

        self._oldChangeSet = None
        self._oldTroveCSets = None
        self._newVersions = None

    def addPackage(self, package):
        self._packages[package.getName()] = package

    def shadow(self, helper, mangleData):
        if not self._packages:
            # Short-circuit
            return

        self._packageList = sorted(self._packages.itervalues())
        self._helper, self._mangleData = helper, mangleData

        self._getUpstreamChangeSets()
        self._mangleRecipes()

        self._getOldChangeSets()

        self._commitShadows()

    def _getUpstreamChangeSets(self):
        '''
        Get trove changesets of all upstream source troves.
        '''

        job = []
        for package in self._packageList:
            job.append((package.getName(), (None, None),
                (package.getUpstreamVersion(), deps.Flavor()), True))

        self._upstreamChangeSet = self._helper.getRepos().createChangeSet(job,
            withFileContents=False, recurse=False)

        self._upstreamTroveCSets = []
        for package in self._packageList:
            troveCs = self._upstreamChangeSet.getNewTroveVersion(
                *package.getUpstreamNameVersionFlavor())
            self._upstreamTroveCSets.append(troveCs)

    def _mangleRecipes(self):
        '''
        Fetch the recipes of all upstream sources, mangle them, load
        the recipe, and save the mangled recipe and new revision.
        '''

        # Get recipe contents
        fileJob = []
        for package, troveCs \
          in zip(self._packageList, self._upstreamTroveCSets):
            fileId, fileVer = findFile(troveCs, package.getRecipeName())[2:4]
            fileJob.append((fileId, fileVer))

        results = self._helper.getRepos().getFileContents(fileJob)

        # Mangle and load each recipe
        self._recipes = []
        self._recipeObjs = []
        for package, contents in zip(self._packageList, results):
            log.debug('Loading %s', package.getName())

            # Get contents
            recipe = contents.get().read()

            # Mangle
            package.setMangleData(self._mangleData)
            finalRecipe = mangle(package, recipe)

            # Write to disk for convenience, then load
            tempDir = tempfile.mkdtemp(prefix=('%s-'
                % package.getPackageName()))
            try:
                recipePath = os.path.join(tempDir, package.getRecipeName())
                open(recipePath, 'w').write(finalRecipe)

                recipeObj = _loadRecipe(self._helper, package, recipePath)
            finally:
                shutil.rmtree(tempDir)

            self._recipes.append(finalRecipe)
            self._recipeObjs.append(recipeObj)

    def _getOldChangeSets(self):
        '''
        Get trove changesets of previous versions of each downstream
        trove with the same revision as the new one.
        '''
        targetLabel = self._helper.plan.targetLabel

        # Look for an existing trove with the same revision
        queries = []
        for package, recipeObj in zip(self._packageList, self._recipeObjs):
            version = '%s/%s' % (targetLabel, recipeObj.version)
            queries.append((package.getName(), version, None))

        results = self._helper.getRepos().findTroves(None, queries,
            allowMissing=True)

        job = []
        oldQueries = []
        self._newVersions = []
        for package, recipeObj, query in zip(self._packageList,
          self._recipeObjs, queries):
            targetBranch = _getTargetBranch(package, targetLabel)
            if query in results:
                # An old version was found.
                assert len(results[query]) == 1
                oldVersion = results[query][0][1]

                # Check that it is on the correct branch.
                if oldVersion.branch() == targetBranch:
                    newVersion = oldVersion.copy()
                    newVersion.incrementSourceCount()

                    # Add the old trove to the batch of troves to fetch
                    # below.
                    job.append((package.getName(), (None, None),
                        (oldVersion, deps.Flavor()), True))
                    oldQueries.append((package.getName(), oldVersion,
                        deps.Flavor()))

                    self._newVersions.append(newVersion)
                    continue

            # No old version exists. Create one.
            newVersion = _createVersion(package, self._helper,
                recipeObj.version)
            oldQueries.append(None)
            self._newVersions.append(newVersion)

        # Now collect all the predecessor troves for comparison
        # purposes
        self._oldChangeSet = self._helper.getRepos().createChangeSet(job,
            withFileContents=False, recurse=False)

        self._oldTroveCSets = []
        for package, oldQuery in zip(self._packageList, oldQueries):
            if oldQuery:
                troveCs = self._oldChangeSet.getNewTroveVersion(*oldQuery)
                self._oldTroveCSets.append(troveCs)
            else:
                self._oldTroveCSets.append(None)

    def _commitShadows(self):
        '''
        Check if each source needs shadowing; if so, add it to a
        changeset and commit it at the end.
        '''

        changeSet = ChangeSet()
        doCommit = False

        for package, newVersion, recipe, recipeObj, \
          upstreamTroveCs, oldTroveCs \
          in zip(self._packageList, self._newVersions,
          self._recipes, self._recipeObjs,
          self._upstreamTroveCSets, self._oldTroveCSets):
            # Figure out what auto-sources we're using
            hgSource = autosource.getHgSource(oldTroveCs, recipeObj)

            # Check if the existing trove is recent enough
            if not hgSource and oldTroveCs and _sourcesIdentical(oldTroveCs,
              self._oldChangeSet, upstreamTroveCs,
              package.getRecipeName(), recipe):
                # Looks like it is. Keep the old version.
                keepVersion = oldTroveCs.getNewVersion()
                package.setDownstreamVersion(keepVersion)
                log.debug('Keeping %s=%s', package.getName(), keepVersion)
                continue

            # Otherwise, build a trove and add it to the changeset.
            # Use the upstream trove as a starting point so we don't
            # have to add any files other than the recipe itself.
            newTrove = Trove(upstreamTroveCs)
            newTrove.changeVersion(newVersion)

            # Create a filestream for the recipe
            recipeFileHelper = filetypes.RegularFile(contents=recipe,
                config=True)
            recipePathId = findFile(upstreamTroveCs,
                package.getRecipeName())[0]
            recipeFile = recipeFileHelper.get(recipePathId)
            recipeFile.flags.isSource(set=True)
            recipeFileId = recipeFile.fileId()

            # Add the recipe to the changeset
            changeSet.addFileContents(recipePathId, recipeFileId,
                ChangedFileTypes.file, recipeFileHelper.contents,
                cfgFile=True)
            changeSet.addFile(None, recipeFileId, recipeFile.freeze())

            # Replace the recipe in the trove with the new one
            newTrove.removeFile(recipePathId)
            newTrove.addFile(recipePathId, package.getRecipeName(),
                newVersion, recipeFileId)

            # If an autosource is involved, add that and remove
            # any existing source.
            if hgSource:
                autosource.addSnapshotToTrove(changeSet, newTrove,
                    recipeObj, hgSource)

            # Create a changelog entry.
            changeLog = ChangeLog(
                name=self._helper.cfg.name, contact=self._helper.cfg.contact,
                message=self._helper.plan.commitMessage + '\n')
            newTrove.changeChangeLog(changeLog)

            # Calculate trove digests and add the trove to the changeset
            newTrove.invalidateDigests()
            newTrove.computeDigests()
            newTroveCs = newTrove.diff(None, absolute=True)[0]
            changeSet.newTrove(newTroveCs)
            doCommit = True

            package.setDownstreamVersion(newVersion)
            log.debug('Committed %s=%s', package.getName(), newVersion)
            # TODO: maybe save the downstream trove object for group recursion

        # Commit!
        if doCommit:
            if compat.ConaryVersion().signAfterPromote():
                cook.signAbsoluteChangeset(changeSet, None)
            self._helper.getRepos().commitChangeSet(changeSet)


def _getTargetBranch(package, targetLabel):
    config = package.getTargetConfig()
    siblingClone = config and config.siblingClone

    sourceBranch = package.getUpstreamVersion().branch()
    if not siblingClone:
        return sourceBranch.createShadow(targetLabel)
    else:
        return sourceBranch.createSibling(targetLabel)


def _createVersion(package, helper, version):
    '''
    Pick a new version for package I{package} using I{version} as the
    new upstream version.
    '''

    targetLabel = helper.plan.targetLabel
    config = package.getTargetConfig()
    siblingClone = config and config.siblingClone

    sourceVersion = package.getUpstreamVersion()
    sourceBranch = sourceVersion.branch()
    sourceRevision = sourceVersion.trailingRevision()

    if siblingClone:
        # Siblings should just start with -1. Use -0 here and
        # increment it below.
        newBranch = sourceBranch.createSibling(helper.plan.targetLabel)
        newRevision = Revision('%s-0' % version)
        newVersion = newBranch.createVersion(newRevision)
    elif sourceRevision.version == version:
        # If shadowing and the upstream versions match, then start
        # with the source version's source count.
        newVersion = sourceVersion.createShadow(targetLabel)
    else:
        # Otherwise create one with a "modified upstream version."
        # ex. 1.2.3-0.1
        newBranch = sourceBranch.createShadow(helper.plan.targetLabel)
        newRevision = Revision('%s-0' % version)
        newVersion = newBranch.createVersion(newRevision)
    newVersion.incrementSourceCount()

    return newVersion


def _sourcesIdentical(oldTroveCs, oldCs, newTroveCs, recipeName, newRecipe):
    '''
    Compare the trove changesets I{oldTroveCs} and I{newTroveCs}.
    Return I{True} if I{oldTroveCs} is identical to I{newTroveCs} with
    the file I{recipeName} replaced with contents I{recipe} in the
    latter.
    '''

    # First check everything but the recipe. Just compare fileIds
    # since all we do with these files is copy them intact.
    newList = set(fileId for (_, path, fileId, _)
        in newTroveCs.getNewFileList() if path != recipeName)
    oldList = set(fileId for (_, path, fileId, _)
        in oldTroveCs.getNewFileList() if path != recipeName)
    if newList != oldList:
        return False

    # Now check the recipes. Here, we'll have to compare SHA-1 digests.
    # We can't create a new file out of the new recipe and compare
    # fileIds because we'd have to clone the inode info in order to get
    # the fileId to line up, which would be silly.
    oldPathId, oldFileId = [(pathId, fileId) for (pathId, path, fileId, _)
        in oldTroveCs.getNewFileList() if path == recipeName][0]
    oldFile = ThawFile(oldCs.getFileChange(None, oldFileId), oldPathId)
    oldDigest = oldFile.contents.sha1()
    newDigest = sha1String(newRecipe)
    return oldDigest == newDigest


def _loadRecipe(helper, package, recipePath):
    # Load the recipe
    buildFlavor = sorted(package.getFlavors())[0]
    use.setBuildFlagsFromFlavor(package.getPackageName(), buildFlavor,
        error=False)
    loader = RecipeLoader(recipePath, helper.cfg, helper.getRepos())
    recipeClass = loader.getRecipe()

    # Instantiate and setup if a package recipe
    if isPackageRecipe(recipeClass):
        lcache = RepositoryCache(helper.getRepos())
        macros = {'buildlabel': helper.plan.sourceLabel.asString(),
            'buildbranch': package.getUpstreamVersion().branch().asString()}
        recipeObj = recipeClass(helper.cfg, lcache, [], macros, lightInstance=True)
        recipeObj.sourceVersion = package.getUpstreamVersion()
        recipeObj.populateLcache()
        if not recipeObj.needsCrossFlags():
            recipeObj.crossRequires = []
        recipeObj.loadPolicy()
        recipeObj.setup()
        return recipeObj

    # Just the class is enough for everything else
    return recipeClass