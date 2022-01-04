import os
import re
import sys
import six
import platform
from collections import OrderedDict


from avalon import api, io, lib
import avalon.nuke
from avalon.nuke import lib as anlib
from avalon.nuke import (
    save_file, open_file
)
from openpype.api import (
    Logger,
    Anatomy,
    BuildWorkfile,
    get_version_from_path,
    get_anatomy_settings,
    get_workdir_data,
    get_asset,
    get_current_project_settings,
    ApplicationManager
)
from openpype.tools.utils import host_tools
from openpype.lib.path_tools import HostDirmap
from openpype.settings import get_project_settings
from openpype.modules import ModulesManager

import nuke

from .utils import set_context_favorites

log = Logger().get_logger(__name__)

opnl = sys.modules[__name__]
opnl._project = None
opnl.project_name = os.getenv("AVALON_PROJECT")
opnl.workfiles_launched = False
opnl._node_tab_name = "{}".format(os.getenv("AVALON_LABEL") or "Avalon")


def get_nuke_imageio_settings():
    return get_anatomy_settings(opnl.project_name)["imageio"]["nuke"]


def get_created_node_imageio_setting(**kwarg):
    ''' Get preset data for dataflow (fileType, compression, bitDepth)
    '''
    log.debug(kwarg)
    nodeclass = kwarg.get("nodeclass", None)
    creator = kwarg.get("creator", None)

    assert any([creator, nodeclass]), nuke.message(
        "`{}`: Missing mandatory kwargs `host`, `cls`".format(__file__))

    imageio_nodes = get_nuke_imageio_settings()["nodes"]["requiredNodes"]

    imageio_node = None
    for node in imageio_nodes:
        log.info(node)
        if (nodeclass in node["nukeNodeClass"]) and (
                creator in node["plugins"]):
            imageio_node = node
            break

    log.info("ImageIO node: {}".format(imageio_node))
    return imageio_node


def get_imageio_input_colorspace(filename):
    ''' Get input file colorspace based on regex in settings.
    '''
    imageio_regex_inputs = (
        get_nuke_imageio_settings()["regexInputs"]["inputs"])

    preset_clrsp = None
    for regexInput in imageio_regex_inputs:
        if bool(re.search(regexInput["regex"], filename)):
            preset_clrsp = str(regexInput["colorspace"])

    return preset_clrsp


def on_script_load():
    ''' Callback for ffmpeg support
    '''
    if nuke.env['LINUX']:
        nuke.tcl('load ffmpegReader')
        nuke.tcl('load ffmpegWriter')
    else:
        nuke.tcl('load movReader')
        nuke.tcl('load movWriter')


def check_inventory_versions():
    """
    Actual version idetifier of Loaded containers

    Any time this function is run it will check all nodes and filter only
    Loader nodes for its version. It will get all versions from database
    and check if the node is having actual version. If not then it will color
    it to red.
    """
    # get all Loader nodes by avalon attribute metadata
    for each in nuke.allNodes():
        container = avalon.nuke.parse_container(each)

        if container:
            node = nuke.toNode(container["objectName"])
            avalon_knob_data = avalon.nuke.read(
                node)

            # get representation from io
            representation = io.find_one({
                "type": "representation",
                "_id": io.ObjectId(avalon_knob_data["representation"])
            })

            # Failsafe for not finding the representation.
            if not representation:
                log.warning(
                    "Could not find the representation on "
                    "node \"{}\"".format(node.name())
                )
                continue

            # Get start frame from version data
            version = io.find_one({
                "type": "version",
                "_id": representation["parent"]
            })

            # get all versions in list
            versions = io.find({
                "type": "version",
                "parent": version["parent"]
            }).distinct('name')

            max_version = max(versions)

            # check the available version and do match
            # change color of node if not max verion
            if version.get("name") not in [max_version]:
                node["tile_color"].setValue(int("0xd84f20ff", 16))
            else:
                node["tile_color"].setValue(int("0x4ecd25ff", 16))


def writes_version_sync():
    ''' Callback synchronizing version of publishable write nodes
    '''
    try:
        rootVersion = get_version_from_path(nuke.root().name())
        padding = len(rootVersion)
        new_version = "v" + str("{" + ":0>{}".format(padding) + "}").format(
            int(rootVersion)
        )
        log.debug("new_version: {}".format(new_version))
    except Exception:
        return

    for each in nuke.allNodes(filter="Write"):
        # check if the node is avalon tracked
        if opnl._node_tab_name not in each.knobs():
            continue

        avalon_knob_data = avalon.nuke.read(
            each)

        try:
            if avalon_knob_data['families'] not in ["render"]:
                log.debug(avalon_knob_data['families'])
                continue

            node_file = each['file'].value()

            node_version = "v" + get_version_from_path(node_file)
            log.debug("node_version: {}".format(node_version))

            node_new_file = node_file.replace(node_version, new_version)
            each['file'].setValue(node_new_file)
            if not os.path.isdir(os.path.dirname(node_new_file)):
                log.warning("Path does not exist! I am creating it.")
                os.makedirs(os.path.dirname(node_new_file))
        except Exception as e:
            log.warning(
                "Write node: `{}` has no version in path: {}".format(
                    each.name(), e))


def version_up_script():
    ''' Raising working script's version
    '''
    import nukescripts
    nukescripts.script_and_write_nodes_version_up()


def check_subsetname_exists(nodes, subset_name):
    """
    Checking if node is not already created to secure there is no duplicity

    Arguments:
        nodes (list): list of nuke.Node objects
        subset_name (str): name we try to find

    Returns:
        bool: True of False
    """
    return next((True for n in nodes
                 if subset_name in avalon.nuke.read(n).get("subset", "")),
                False)


def get_render_path(node):
    ''' Generate Render path from presets regarding avalon knob data
    '''
    data = {'avalon': avalon.nuke.read(node)}
    data_preset = {
        "nodeclass": data['avalon']['family'],
        "families": [data['avalon']['families']],
        "creator": data['avalon']['creator']
    }

    nuke_imageio_writes = get_created_node_imageio_setting(**data_preset)

    application = lib.get_application(os.environ["AVALON_APP_NAME"])
    data.update({
        "application": application,
        "nuke_imageio_writes": nuke_imageio_writes
    })

    anatomy_filled = format_anatomy(data)
    return anatomy_filled["render"]["path"].replace("\\", "/")


def format_anatomy(data):
    ''' Helping function for formating of anatomy paths

    Arguments:
        data (dict): dictionary with attributes used for formating

    Return:
        path (str)
    '''
    # TODO: perhaps should be nonPublic

    anatomy = Anatomy()
    log.debug("__ anatomy.templates: {}".format(anatomy.templates))

    try:
        # TODO: bck compatibility with old anatomy template
        padding = int(
            anatomy.templates["render"].get(
                "frame_padding",
                anatomy.templates["render"].get("padding")
            )
        )
    except KeyError as e:
        msg = ("`padding` key is not in `render` "
               "or `frame_padding` on is not available in "
               "Anatomy template. Please, add it there and restart "
               "the pipeline (padding: \"4\"): `{}`").format(e)

        log.error(msg)
        nuke.message(msg)

    version = data.get("version", None)
    if not version:
        file = script_name()
        data["version"] = get_version_from_path(file)

    project_doc = io.find_one({"type": "project"})
    asset_doc = io.find_one({
        "type": "asset",
        "name": data["avalon"]["asset"]
    })
    task_name = os.environ["AVALON_TASK"]
    host_name = os.environ["AVALON_APP"]
    context_data = get_workdir_data(
        project_doc, asset_doc, task_name, host_name
    )
    data.update(context_data)
    data.update({
        "subset": data["avalon"]["subset"],
        "family": data["avalon"]["family"],
        "frame": "#" * padding,
    })
    return anatomy.format(data)


def script_name():
    ''' Returns nuke script path
    '''
    return nuke.root().knob('name').value()


def add_button_write_to_read(node):
    name = "createReadNode"
    label = "Create Read From Rendered"
    value = "import write_to_read;\
        write_to_read.write_to_read(nuke.thisNode(), allow_relative=False)"
    knob = nuke.PyScript_Knob(name, label, value)
    knob.clearFlag(nuke.STARTLINE)
    node.addKnob(knob)


def create_write_node(name, data, input=None, prenodes=None,
                      review=True, linked_knobs=None, farm=True):
    ''' Creating write node which is group node

    Arguments:
        name (str): name of node
        data (dict): data to be imprinted
        input (node): selected node to connect to
        prenodes (list, optional): list of lists, definitions for nodes
                                to be created before write
        review (bool): adding review knob

    Example:
        prenodes = [
            {
                "nodeName": {
                    "class": ""  # string
                    "knobs": [
                        ("knobName": value),
                        ...
                    ],
                    "dependent": [
                        following_node_01,
                        ...
                    ]
                }
            },
            ...
        ]

    Return:
        node (obj): group node with avalon data as Knobs
    '''

    imageio_writes = get_created_node_imageio_setting(**data)
    app_manager = ApplicationManager()
    app_name = os.environ.get("AVALON_APP_NAME")
    if app_name:
        app = app_manager.applications.get(app_name)

    for knob in imageio_writes["knobs"]:
        if knob["name"] == "file_type":
            representation = knob["value"]

    try:
        data.update({
            "app": app.host_name,
            "imageio_writes": imageio_writes,
            "representation": representation,
        })
        anatomy_filled = format_anatomy(data)

    except Exception as e:
        msg = "problem with resolving anatomy template: {}".format(e)
        log.error(msg)
        nuke.message(msg)

    # build file path to workfiles
    fdir = str(anatomy_filled["work"]["folder"]).replace("\\", "/")
    fpath = data["fpath_template"].format(
        work=fdir, version=data["version"], subset=data["subset"],
        frame=data["frame"],
        ext=representation
    )

    # create directory
    if not os.path.isdir(os.path.dirname(fpath)):
        log.warning("Path does not exist! I am creating it.")
        os.makedirs(os.path.dirname(fpath))

    _data = OrderedDict({
        "file": fpath
    })

    # adding dataflow template
    log.debug("imageio_writes: `{}`".format(imageio_writes))
    for knob in imageio_writes["knobs"]:
        _data.update({knob["name"]: knob["value"]})

    _data = anlib.fix_data_for_node_create(_data)

    log.debug("_data: `{}`".format(_data))

    if "frame_range" in data.keys():
        _data["frame_range"] = data.get("frame_range", None)
        log.debug("_data[frame_range]: `{}`".format(_data["frame_range"]))

    GN = nuke.createNode("Group", "name {}".format(name))

    prev_node = None
    with GN:
        if input:
            input_name = str(input.name()).replace(" ", "")
            # if connected input node was defined
            prev_node = nuke.createNode(
                "Input", "name {}".format(input_name))
        else:
            # generic input node connected to nothing
            prev_node = nuke.createNode(
                "Input", "name {}".format("rgba"))
        prev_node.hideControlPanel()
        # creating pre-write nodes `prenodes`
        if prenodes:
            for node in prenodes:
                # get attributes
                pre_node_name = node["name"]
                klass = node["class"]
                knobs = node["knobs"]
                dependent = node["dependent"]

                # create node
                now_node = nuke.createNode(
                    klass, "name {}".format(pre_node_name))
                now_node.hideControlPanel()

                # add data to knob
                for _knob in knobs:
                    knob, value = _knob
                    try:
                        now_node[knob].value()
                    except NameError:
                        log.warning(
                            "knob `{}` does not exist on node `{}`".format(
                                knob, now_node["name"].value()
                            ))
                        continue

                    if not knob and not value:
                        continue

                    log.info((knob, value))

                    if isinstance(value, str):
                        if "[" in value:
                            now_node[knob].setExpression(value)
                    else:
                        now_node[knob].setValue(value)

                # connect to previous node
                if dependent:
                    if isinstance(dependent, (tuple or list)):
                        for i, node_name in enumerate(dependent):
                            input_node = nuke.createNode(
                                "Input", "name {}".format(node_name))
                            input_node.hideControlPanel()
                            now_node.setInput(1, input_node)

                    elif isinstance(dependent, str):
                        input_node = nuke.createNode(
                            "Input", "name {}".format(node_name))
                        input_node.hideControlPanel()
                        now_node.setInput(0, input_node)

                else:
                    now_node.setInput(0, prev_node)

                # swith actual node to previous
                prev_node = now_node

        # creating write node
        write_node = now_node = anlib.add_write_node(
            "inside_{}".format(name),
            **_data
        )
        write_node.hideControlPanel()
        # connect to previous node
        now_node.setInput(0, prev_node)

        # swith actual node to previous
        prev_node = now_node

        now_node = nuke.createNode("Output", "name Output1")
        now_node.hideControlPanel()

        # connect to previous node
        now_node.setInput(0, prev_node)

    # imprinting group node
    anlib.set_avalon_knob_data(GN, data["avalon"])
    anlib.add_publish_knob(GN)
    add_rendering_knobs(GN, farm)

    if review:
        add_review_knob(GN)

    # add divider
    GN.addKnob(nuke.Text_Knob('', 'Rendering'))

    # Add linked knobs.
    linked_knob_names = []

    # add input linked knobs and create group only if any input
    if linked_knobs:
        linked_knob_names.append("_grp-start_")
        linked_knob_names.extend(linked_knobs)
        linked_knob_names.append("_grp-end_")

    linked_knob_names.append("Render")

    for _k_name in linked_knob_names:
        if "_grp-start_" in _k_name:
            knob = nuke.Tab_Knob(
                "rnd_attr", "Rendering attributes", nuke.TABBEGINCLOSEDGROUP)
            GN.addKnob(knob)
        elif "_grp-end_" in _k_name:
            knob = nuke.Tab_Knob(
                "rnd_attr_end", "Rendering attributes", nuke.TABENDGROUP)
            GN.addKnob(knob)
        else:
            if "___" in _k_name:
                # add devider
                GN.addKnob(nuke.Text_Knob(""))
            else:
                # add linked knob by _k_name
                link = nuke.Link_Knob("")
                link.makeLink(write_node.name(), _k_name)
                link.setName(_k_name)

                # make render
                if "Render" in _k_name:
                    link.setLabel("Render Local")
                link.setFlag(0x1000)
                GN.addKnob(link)

    # adding write to read button
    add_button_write_to_read(GN)

    # Deadline tab.
    add_deadline_tab(GN)

    # open the our Tab as default
    GN[opnl._node_tab_name].setFlag(0)

    # set tile color
    tile_color = _data.get("tile_color", "0xff0000ff")
    GN["tile_color"].setValue(tile_color)

    return GN


def add_rendering_knobs(node, farm=True):
    ''' Adds additional rendering knobs to given node

    Arguments:
        node (obj): nuke node object to be fixed

    Return:
        node (obj): with added knobs
    '''
    knob_options = ["Use existing frames", "Local"]
    if farm:
        knob_options.append("On farm")

    if "render" not in node.knobs():
        knob = nuke.Enumeration_Knob("render", "", knob_options)
        knob.clearFlag(nuke.STARTLINE)
        node.addKnob(knob)
    return node


def add_review_knob(node):
    ''' Adds additional review knob to given node

    Arguments:
        node (obj): nuke node object to be fixed

    Return:
        node (obj): with added knob
    '''
    if "review" not in node.knobs():
        knob = nuke.Boolean_Knob("review", "Review")
        knob.setValue(True)
        node.addKnob(knob)
    return node


def add_deadline_tab(node):
    node.addKnob(nuke.Tab_Knob("Deadline"))

    knob = nuke.Int_Knob("deadlineChunkSize", "Chunk Size")
    knob.setValue(0)
    node.addKnob(knob)

    knob = nuke.Int_Knob("deadlinePriority", "Priority")
    knob.setValue(50)
    node.addKnob(knob)


def get_deadline_knob_names():
    return ["Deadline", "deadlineChunkSize", "deadlinePriority"]


def create_backdrop(label="", color=None, layer=0,
                    nodes=None):
    """
    Create Backdrop node

    Arguments:
        color (str): nuke compatible string with color code
        layer (int): layer of node usually used (self.pos_layer - 1)
        label (str): the message
        nodes (list): list of nodes to be wrapped into backdrop

    """
    assert isinstance(nodes, list), "`nodes` should be a list of nodes"

    # Calculate bounds for the backdrop node.
    bdX = min([node.xpos() for node in nodes])
    bdY = min([node.ypos() for node in nodes])
    bdW = max([node.xpos() + node.screenWidth() for node in nodes]) - bdX
    bdH = max([node.ypos() + node.screenHeight() for node in nodes]) - bdY

    # Expand the bounds to leave a little border. Elements are offsets
    # for left, top, right and bottom edges respectively
    left, top, right, bottom = (-20, -65, 20, 60)
    bdX += left
    bdY += top
    bdW += (right - left)
    bdH += (bottom - top)

    bdn = nuke.createNode("BackdropNode")
    bdn["z_order"].setValue(layer)

    if color:
        bdn["tile_color"].setValue(int(color, 16))

    bdn["xpos"].setValue(bdX)
    bdn["ypos"].setValue(bdY)
    bdn["bdwidth"].setValue(bdW)
    bdn["bdheight"].setValue(bdH)

    if label:
        bdn["label"].setValue(label)

    bdn["note_font_size"].setValue(20)
    return bdn


class WorkfileSettings(object):
    """
    All settings for workfile will be set

    This object is setting all possible root settings to the workfile.
    Including Colorspace, Frame ranges, Resolution format. It can set it
    to Root node or to any given node.

    Arguments:
        root (node): nuke's root node
        nodes (list): list of nuke's nodes
        nodes_filter (list): filtering classes for nodes

    """

    def __init__(self,
                 root_node=None,
                 nodes=None,
                 **kwargs):
        opnl._project = kwargs.get(
            "project") or io.find_one({"type": "project"})
        self._asset = kwargs.get("asset_name") or api.Session["AVALON_ASSET"]
        self._asset_entity = get_asset(self._asset)
        self._root_node = root_node or nuke.root()
        self._nodes = self.get_nodes(nodes=nodes)

        self.data = kwargs

    def get_nodes(self, nodes=None, nodes_filter=None):

        if not isinstance(nodes, list) and not isinstance(nodes_filter, list):
            return [n for n in nuke.allNodes()]
        elif not isinstance(nodes, list) and isinstance(nodes_filter, list):
            nodes = list()
            for filter in nodes_filter:
                [nodes.append(n) for n in nuke.allNodes(filter=filter)]
            return nodes
        elif isinstance(nodes, list) and not isinstance(nodes_filter, list):
            return [n for n in self._nodes]
        elif isinstance(nodes, list) and isinstance(nodes_filter, list):
            for filter in nodes_filter:
                return [n for n in self._nodes if filter in n.Class()]

    def set_viewers_colorspace(self, viewer_dict):
        ''' Adds correct colorspace to viewer

        Arguments:
            viewer_dict (dict): adjustments from presets

        '''
        if not isinstance(viewer_dict, dict):
            msg = "set_viewers_colorspace(): argument should be dictionary"
            log.error(msg)
            nuke.message(msg)
            return

        filter_knobs = [
            "viewerProcess",
            "wipe_position"
        ]

        erased_viewers = []
        for v in nuke.allNodes(filter="Viewer"):
            v['viewerProcess'].setValue(str(viewer_dict["viewerProcess"]))
            if str(viewer_dict["viewerProcess"]) \
                    not in v['viewerProcess'].value():
                copy_inputs = v.dependencies()
                copy_knobs = {k: v[k].value() for k in v.knobs()
                              if k not in filter_knobs}

                # delete viewer with wrong settings
                erased_viewers.append(v['name'].value())
                nuke.delete(v)

                # create new viewer
                nv = nuke.createNode("Viewer")

                # connect to original inputs
                for i, n in enumerate(copy_inputs):
                    nv.setInput(i, n)

                # set coppied knobs
                for k, v in copy_knobs.items():
                    print(k, v)
                    nv[k].setValue(v)

                # set viewerProcess
                nv['viewerProcess'].setValue(str(viewer_dict["viewerProcess"]))

        if erased_viewers:
            log.warning(
                "Attention! Viewer nodes {} were erased."
                "It had wrong color profile".format(erased_viewers))

    def set_root_colorspace(self, root_dict):
        ''' Adds correct colorspace to root

        Arguments:
            root_dict (dict): adjustmensts from presets

        '''
        if not isinstance(root_dict, dict):
            msg = "set_root_colorspace(): argument should be dictionary"
            log.error(msg)
            nuke.message(msg)

        log.debug(">> root_dict: {}".format(root_dict))

        # first set OCIO
        if self._root_node["colorManagement"].value() \
                not in str(root_dict["colorManagement"]):
            self._root_node["colorManagement"].setValue(
                str(root_dict["colorManagement"]))
            log.debug("nuke.root()['{0}'] changed to: {1}".format(
                "colorManagement", root_dict["colorManagement"]))
            root_dict.pop("colorManagement")

        # second set ocio version
        if self._root_node["OCIO_config"].value() \
                not in str(root_dict["OCIO_config"]):
            self._root_node["OCIO_config"].setValue(
                str(root_dict["OCIO_config"]))
            log.debug("nuke.root()['{0}'] changed to: {1}".format(
                "OCIO_config", root_dict["OCIO_config"]))
            root_dict.pop("OCIO_config")

        # third set ocio custom path
        if root_dict.get("customOCIOConfigPath"):
            unresolved_path = root_dict["customOCIOConfigPath"]
            ocio_paths = unresolved_path[platform.system().lower()]

            resolved_path = None
            for ocio_p in ocio_paths:
                resolved_path = str(ocio_p).format(**os.environ)
                if not os.path.exists(resolved_path):
                    continue

            if resolved_path:
                self._root_node["customOCIOConfigPath"].setValue(
                    str(resolved_path).replace("\\", "/")
                )
                log.debug("nuke.root()['{}'] changed to: {}".format(
                    "customOCIOConfigPath", resolved_path))
                root_dict.pop("customOCIOConfigPath")

        # then set the rest
        for knob, value in root_dict.items():
            # skip unfilled ocio config path
            # it will be dict in value
            if isinstance(value, dict):
                continue
            if self._root_node[knob].value() not in value:
                self._root_node[knob].setValue(str(value))
                log.debug("nuke.root()['{}'] changed to: {}".format(
                    knob, value))

    def set_writes_colorspace(self):
        ''' Adds correct colorspace to write node dict

        '''
        from avalon.nuke import read

        for node in nuke.allNodes(filter="Group"):

            # get data from avalon knob
            avalon_knob_data = read(node)

            if not avalon_knob_data:
                continue

            if avalon_knob_data["id"] != "pyblish.avalon.instance":
                continue

            # establish families
            families = [avalon_knob_data["family"]]
            if avalon_knob_data.get("families"):
                families.append(avalon_knob_data.get("families"))

            data_preset = {
                "nodeclass": avalon_knob_data["family"],
                "families": families,
                "creator": avalon_knob_data['creator']
            }

            nuke_imageio_writes = get_created_node_imageio_setting(
                **data_preset)

            log.debug("nuke_imageio_writes: `{}`".format(nuke_imageio_writes))

            if not nuke_imageio_writes:
                return

            write_node = None

            # get into the group node
            node.begin()
            for x in nuke.allNodes():
                if x.Class() == "Write":
                    write_node = x
            node.end()

            if not write_node:
                return

            # write all knobs to node
            for knob in nuke_imageio_writes["knobs"]:
                value = knob["value"]
                if isinstance(value, six.text_type):
                    value = str(value)
                if str(value).startswith("0x"):
                    value = int(value, 16)

                write_node[knob["name"]].setValue(value)


    def set_reads_colorspace(self, read_clrs_inputs):
        """ Setting colorspace to Read nodes

        Looping trought all read nodes and tries to set colorspace based
        on regex rules in presets
        """
        changes = {}
        for n in nuke.allNodes():
            file = nuke.filename(n)
            if n.Class() != "Read":
                continue

            # check if any colorspace presets for read is mathing
            preset_clrsp = None

            for input in read_clrs_inputs:
                if not bool(re.search(input["regex"], file)):
                    continue
                preset_clrsp = input["colorspace"]

            log.debug(preset_clrsp)
            if preset_clrsp is not None:
                current = n["colorspace"].value()
                future = str(preset_clrsp)
                if current != future:
                    changes.update({
                        n.name(): {
                            "from": current,
                            "to": future
                        }
                    })
        log.debug(changes)
        if changes:
            msg = "Read nodes are not set to correct colospace:\n\n"
            for nname, knobs in changes.items():
                msg += str(
                    " - node: '{0}' is now '{1}' but should be '{2}'\n"
                ).format(nname, knobs["from"], knobs["to"])

            msg += "\nWould you like to change it?"

            if nuke.ask(msg):
                for nname, knobs in changes.items():
                    n = nuke.toNode(nname)
                    n["colorspace"].setValue(knobs["to"])
                    log.info(
                        "Setting `{0}` to `{1}`".format(
                            nname,
                            knobs["to"]))

    def set_colorspace(self):
        ''' Setting colorpace following presets
        '''
        # get imageio
        nuke_colorspace = get_nuke_imageio_settings()

        try:
            self.set_root_colorspace(nuke_colorspace["workfile"])
        except AttributeError:
            msg = "set_colorspace(): missing `workfile` settings in template"
            nuke.message(msg)

        try:
            self.set_viewers_colorspace(nuke_colorspace["viewer"])
        except AttributeError:
            msg = "set_colorspace(): missing `viewer` settings in template"
            nuke.message(msg)
            log.error(msg)

        try:
            self.set_writes_colorspace()
        except AttributeError as _error:
            nuke.message(_error)
            log.error(_error)

        read_clrs_inputs = nuke_colorspace["regexInputs"].get("inputs", [])
        if read_clrs_inputs:
            self.set_reads_colorspace(read_clrs_inputs)

        try:
            for key in nuke_colorspace:
                log.debug("Preset's colorspace key: {}".format(key))
        except TypeError:
            msg = "Nuke is not in templates! Contact your supervisor!"
            nuke.message(msg)
            log.error(msg)

    def reset_frame_range_handles(self):
        """Set frame range to current asset"""

        if "data" not in self._asset_entity:
            msg = "Asset {} don't have set any 'data'".format(self._asset)
            log.warning(msg)
            nuke.message(msg)
            return
        data = self._asset_entity["data"]

        log.debug("__ asset data: `{}`".format(data))

        missing_cols = []
        check_cols = ["fps", "frameStart", "frameEnd",
                      "handleStart", "handleEnd"]

        for col in check_cols:
            if col not in data:
                missing_cols.append(col)

        if len(missing_cols) > 0:
            missing = ", ".join(missing_cols)
            msg = "'{}' are not set for asset '{}'!".format(
                missing, self._asset)
            log.warning(msg)
            nuke.message(msg)
            return

        # get handles values
        handle_start = data["handleStart"]
        handle_end = data["handleEnd"]

        fps = float(data["fps"])
        frame_start = int(data["frameStart"]) - handle_start
        frame_end = int(data["frameEnd"]) + handle_end

        self._root_node["lock_range"].setValue(False)
        self._root_node["fps"].setValue(fps)
        self._root_node["first_frame"].setValue(frame_start)
        self._root_node["last_frame"].setValue(frame_end)
        self._root_node["lock_range"].setValue(True)

        # setting active viewers
        try:
            nuke.frame(int(data["frameStart"]))
        except Exception as e:
            log.warning("no viewer in scene: `{}`".format(e))

        range = '{0}-{1}'.format(
            int(data["frameStart"]),
            int(data["frameEnd"]))

        for node in nuke.allNodes(filter="Viewer"):
            node['frame_range'].setValue(range)
            node['frame_range_lock'].setValue(True)
            node['frame_range'].setValue(range)
            node['frame_range_lock'].setValue(True)

        # adding handle_start/end to root avalon knob
        if not anlib.set_avalon_knob_data(self._root_node, {
            "handleStart": int(handle_start),
            "handleEnd": int(handle_end)
        }):
            log.warning("Cannot set Avalon knob to Root node!")

    def reset_resolution(self):
        """Set resolution to project resolution."""
        log.info("Reseting resolution")
        project = io.find_one({"type": "project"})
        asset = api.Session["AVALON_ASSET"]
        asset = io.find_one({"name": asset, "type": "asset"})
        asset_data = asset.get('data', {})

        data = {
            "width": int(asset_data.get(
                'resolutionWidth',
                asset_data.get('resolution_width'))),
            "height": int(asset_data.get(
                'resolutionHeight',
                asset_data.get('resolution_height'))),
            "pixel_aspect": asset_data.get(
                'pixelAspect',
                asset_data.get('pixel_aspect', 1)),
            "name": project["name"]
        }

        if any(x for x in data.values() if x is None):
            msg = ("Missing set shot attributes in DB."
                   "\nContact your supervisor!."
                   "\n\nWidth: `{width}`"
                   "\nHeight: `{height}`"
                   "\nPixel Asspect: `{pixel_aspect}`").format(**data)
            log.error(msg)
            nuke.message(msg)

        existing_format = None
        for format in nuke.formats():
            if data["name"] == format.name():
                existing_format = format
                break

        if existing_format:
            # Enforce existing format to be correct.
            existing_format.setWidth(data["width"])
            existing_format.setHeight(data["height"])
            existing_format.setPixelAspect(data["pixel_aspect"])
        else:
            format_string = self.make_format_string(**data)
            log.info("Creating new format: {}".format(format_string))
            nuke.addFormat(format_string)

        nuke.root()["format"].setValue(data["name"])
        log.info("Format is set.")

    def make_format_string(self, **kwargs):
        if kwargs.get("r"):
            return (
                "{width} "
                "{height} "
                "{x} "
                "{y} "
                "{r} "
                "{t} "
                "{pixel_aspect:.2f} "
                "{name}".format(**kwargs)
            )
        else:
            return (
                "{width} "
                "{height} "
                "{pixel_aspect:.2f} "
                "{name}".format(**kwargs)
            )

    def set_context_settings(self):
        # replace reset resolution from avalon core to pype's
        self.reset_resolution()
        # replace reset resolution from avalon core to pype's
        self.reset_frame_range_handles()
        # add colorspace menu item
        self.set_colorspace()

    def set_favorites(self):
        work_dir = os.getenv("AVALON_WORKDIR")
        asset = os.getenv("AVALON_ASSET")
        favorite_items = OrderedDict()

        # project
        # get project's root and split to parts
        projects_root = os.path.normpath(work_dir.split(
            opnl.project_name)[0])
        # add project name
        project_dir = os.path.join(projects_root, opnl.project_name) + "/"
        # add to favorites
        favorite_items.update({"Project dir": project_dir.replace("\\", "/")})

        # asset
        asset_root = os.path.normpath(work_dir.split(
            asset)[0])
        # add asset name
        asset_dir = os.path.join(asset_root, asset) + "/"
        # add to favorites
        favorite_items.update({"Shot dir": asset_dir.replace("\\", "/")})

        # workdir
        favorite_items.update({"Work dir": work_dir.replace("\\", "/")})

        set_context_favorites(favorite_items)


def get_hierarchical_attr(entity, attr, default=None):
    attr_parts = attr.split('.')
    value = entity
    for part in attr_parts:
        value = value.get(part)
        if not value:
            break

    if value or entity['type'].lower() == 'project':
        return value

    parent_id = entity['parent']
    if (
        entity['type'].lower() == 'asset'
        and entity.get('data', {}).get('visualParent')
    ):
        parent_id = entity['data']['visualParent']

    parent = io.find_one({'_id': parent_id})

    return get_hierarchical_attr(parent, attr)


def get_write_node_template_attr(node):
    ''' Gets all defined data from presets

    '''
    # get avalon data from node
    data = dict()
    data['avalon'] = avalon.nuke.read(
        node)
    data_preset = {
        "nodeclass": data['avalon']['family'],
        "families": [data['avalon']['families']],
        "creator": data['avalon']['creator']
    }

    # get template data
    nuke_imageio_writes = get_created_node_imageio_setting(**data_preset)

    # collecting correct data
    correct_data = OrderedDict({
        "file": get_render_path(node)
    })

    # adding imageio template
    {correct_data.update({k: v})
     for k, v in nuke_imageio_writes.items()
     if k not in ["_id", "_previous"]}

    # fix badly encoded data
    return anlib.fix_data_for_node_create(correct_data)


def get_dependent_nodes(nodes):
    """Get all dependent nodes connected to the list of nodes.

    Looking for connections outside of the nodes in incoming argument.

    Arguments:
        nodes (list): list of nuke.Node objects

    Returns:
        connections_in: dictionary of nodes and its dependencies
        connections_out: dictionary of nodes and its dependency
    """

    connections_in = dict()
    connections_out = dict()
    node_names = [n.name() for n in nodes]
    for node in nodes:
        inputs = node.dependencies()
        outputs = node.dependent()
        # collect all inputs outside
        test_in = [(i, n) for i, n in enumerate(inputs)
                   if n.name() not in node_names]
        if test_in:
            connections_in.update({
                node: test_in
            })
        # collect all outputs outside
        test_out = [i for i in outputs if i.name() not in node_names]
        if test_out:
            # only one dependent node is allowed
            connections_out.update({
                node: test_out[-1]
            })

    return connections_in, connections_out


def find_free_space_to_paste_nodes(
        nodes,
        group=nuke.root(),
        direction="right",
        offset=300):
    """
    For getting coordinates in DAG (node graph) for placing new nodes

    Arguments:
        nodes (list): list of nuke.Node objects
        group (nuke.Node) [optional]: object in which context it is
        direction (str) [optional]: where we want it to be placed
                                    [left, right, top, bottom]
        offset (int) [optional]: what offset it is from rest of nodes

    Returns:
        xpos (int): x coordinace in DAG
        ypos (int): y coordinace in DAG
    """
    if len(nodes) == 0:
        return 0, 0

    group_xpos = list()
    group_ypos = list()

    # get local coordinates of all nodes
    nodes_xpos = [n.xpos() for n in nodes] + \
                 [n.xpos() + n.screenWidth() for n in nodes]

    nodes_ypos = [n.ypos() for n in nodes] + \
                 [n.ypos() + n.screenHeight() for n in nodes]

    # get complete screen size of all nodes to be placed in
    nodes_screen_width = max(nodes_xpos) - min(nodes_xpos)
    nodes_screen_heigth = max(nodes_ypos) - min(nodes_ypos)

    # get screen size (r,l,t,b) of all nodes in `group`
    with group:
        group_xpos = [n.xpos() for n in nuke.allNodes() if n not in nodes] + \
                     [n.xpos() + n.screenWidth() for n in nuke.allNodes()
                      if n not in nodes]
        group_ypos = [n.ypos() for n in nuke.allNodes() if n not in nodes] + \
                     [n.ypos() + n.screenHeight() for n in nuke.allNodes()
                      if n not in nodes]

        # calc output left
        if direction in "left":
            xpos = min(group_xpos) - abs(nodes_screen_width) - abs(offset)
            ypos = min(group_ypos)
            return xpos, ypos
        # calc output right
        if direction in "right":
            xpos = max(group_xpos) + abs(offset)
            ypos = min(group_ypos)
            return xpos, ypos
        # calc output top
        if direction in "top":
            xpos = min(group_xpos)
            ypos = min(group_ypos) - abs(nodes_screen_heigth) - abs(offset)
            return xpos, ypos
        # calc output bottom
        if direction in "bottom":
            xpos = min(group_xpos)
            ypos = max(group_ypos) + abs(offset)
            return xpos, ypos


def launch_workfiles_app():
    '''Function letting start workfiles after start of host
    '''
    from openpype.lib import (
        env_value_to_bool
    )
    from avalon.nuke.pipeline import get_main_window

    # get all imortant settings
    open_at_start = env_value_to_bool(
        env_key="OPENPYPE_WORKFILE_TOOL_ON_START",
        default=None)

    # return if none is defined
    if not open_at_start:
        return

    if not opnl.workfiles_launched:
        opnl.workfiles_launched = True
        main_window = get_main_window()
        host_tools.show_workfiles(parent=main_window)


def process_workfile_builder():
    from openpype.lib import (
        env_value_to_bool,
        get_custom_workfile_template
    )

    # get state from settings
    workfile_builder = get_current_project_settings()["nuke"].get(
        "workfile_builder", {})

    # get all imortant settings
    openlv_on = env_value_to_bool(
        env_key="AVALON_OPEN_LAST_WORKFILE",
        default=None)

    # get settings
    createfv_on = workfile_builder.get("create_first_version") or None
    custom_templates = workfile_builder.get("custom_templates") or None
    builder_on = workfile_builder.get("builder_on_start") or None

    last_workfile_path = os.environ.get("AVALON_LAST_WORKFILE")

    # generate first version in file not existing and feature is enabled
    if createfv_on and not os.path.exists(last_workfile_path):
        # get custom template path if any
        custom_template_path = get_custom_workfile_template(
            custom_templates
        )

        # if custom template is defined
        if custom_template_path:
            log.info("Adding nodes from `{}`...".format(
                custom_template_path
            ))
            try:
                # import nodes into current script
                nuke.nodePaste(custom_template_path)
            except RuntimeError:
                raise RuntimeError((
                    "Template defined for project: {} is not working. "
                    "Talk to your manager for an advise").format(
                        custom_template_path))

        # if builder at start is defined
        if builder_on:
            log.info("Building nodes from presets...")
            # build nodes by defined presets
            BuildWorkfile().process()

        log.info("Saving script as version `{}`...".format(
            last_workfile_path
        ))
        # safe file as version
        save_file(last_workfile_path)
        return

    # skip opening of last version if it is not enabled
    if not openlv_on or not os.path.exists(last_workfile_path):
        return

    # to avoid looping of the callback, remove it!
    nuke.removeOnCreate(process_workfile_builder, nodeClass="Root")

    log.info("Opening last workfile...")
    # open workfile
    open_file(last_workfile_path)


def recreate_instance(origin_node, avalon_data=None):
    """Recreate input instance to different data

    Args:
        origin_node (nuke.Node): Nuke node to be recreating from
        avalon_data (dict, optional): data to be used in new node avalon_data

    Returns:
        nuke.Node: newly created node
    """
    knobs_wl = ["render", "publish", "review", "ypos",
                "use_limit", "first", "last"]
    # get data from avalon knobs
    data = anlib.get_avalon_knob_data(
        origin_node)

    # add input data to avalon data
    if avalon_data:
        data.update(avalon_data)

    # capture all node knobs allowed in op_knobs
    knobs_data = {k: origin_node[k].value()
                  for k in origin_node.knobs()
                  for key in knobs_wl
                  if key in k}

    # get node dependencies
    inputs = origin_node.dependencies()
    outputs = origin_node.dependent()

    # remove the node
    nuke.delete(origin_node)

    # create new node
    # get appropriate plugin class
    creator_plugin = None
    for Creator in api.discover(api.Creator):
        if Creator.__name__ == data["creator"]:
            creator_plugin = Creator
            break

    # create write node with creator
    new_node_name = data["subset"]
    new_node = creator_plugin(new_node_name, data["asset"]).process()

    # white listed knobs to the new node
    for _k, _v in knobs_data.items():
        try:
            print(_k, _v)
            new_node[_k].setValue(_v)
        except Exception as e:
            print(e)

    # connect to original inputs
    for i, n in enumerate(inputs):
        new_node.setInput(i, n)

    # connect to outputs
    if len(outputs) > 0:
        for dn in outputs:
            dn.setInput(0, new_node)

    return new_node


class NukeDirmap(HostDirmap):
    def __init__(self, project_settings, sync_module=None):
        """
        Args:
            host_name (str): Nuke
            project_settings (dict): settings of current project
            sync_module (SyncServerModule): to limit reinitialization
            file_name (str): full path of referenced file from workfiles
        """
        super(NukeDirmap, self).__init__("nuke", project_settings, sync_module)

    def process_dirmap(self, filepath):
        if os.path.exists(filepath):
            return filepath

        mapping = self.mapping
        if not mapping:
            return filepath

        self.log.info("mapping:: {}".format(mapping))

        filepath = filepath.replace("\\", "/")
        filtered_dst_paths = []
        for dst_path in mapping["destination-path"]:
            if os.path.exists(dst_path):
                filtered_dst_paths.append(dst_path)

        for src_mapping in mapping["source-path"]:
            if not os.path.exists(src_mapping):
                continue

            if not filepath.startswith(src_mapping):
                continue

            for dst_mapping in filtered_dst_paths:
                new_filepath = filepath.replace(src_mapping, dst_mapping)
                if os.path.exists(new_filepath):
                    return new_filepath
        return filepath


class DirmapCache:
    """Caching class to get settings and sync_module easily and only once."""
    _project_settings = None
    _sync_module = None

    @classmethod
    def project_settings(cls):
        if cls._project_settings is None:
            cls._project_settings = get_project_settings(
                os.getenv("AVALON_PROJECT"))
        return cls._project_settings

    @classmethod
    def sync_module(cls):
        if cls._sync_module is None:
            cls._sync_module = ModulesManager().modules_by_name["sync_server"]
        return cls._sync_module


def dirmap_file_name_filter(file_name):
    """Nuke callback function with single full path argument.

        Checks project settings for potential mapping from source to dest.
    """
    dirmap_processor = NukeDirmap("nuke",
                                  DirmapCache.project_settings(),
                                  DirmapCache.sync_module(),
                                  file_name)
    dirmap_processor.process_dirmap()
    if os.path.exists(dirmap_processor.file_name):
        return dirmap_processor.file_name
    return file_name
