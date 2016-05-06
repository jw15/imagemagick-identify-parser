#!/usr/bin/python
import distutils.spawn
import json
import os
import re
import sys
import lxml.etree as etree
from xml.etree.ElementTree import ElementTree,SubElement,Element,dump,tostring

"""
Check if deps are present (ImageMagick)
"""
def checkProgram(program):
    return distutils.spawn.find_executable(program)

"""
DICOM image metadata indentation-based parser class
"""
class ImageMagickIdentifyParser:

    Data = None
    HISTOGRAM_ELEM="HistogramLevel"
    # RE_GROUPED_ENTRY examples:
    # dcm:DeviceSerialNumber
    RE_GROUPED_ENTRY = r"(?P<prefix>.+?):(?P<name>.*)$"
    # RE_LINE_GENERIC examples:
    # Page geometry: 512x512+0+0 
    RE_LINE_GENERIC = r"^(?P<leading>\s*)(?P<name>.*):(?P<value>\s.*|)$"
    # RE_LINE_HISTO examples:
    # 30489: (  385,  385,  385) #018101810181 gray(0.587472%,0.587472%,0.587472%)
    # 6709: (    0,    0,    0) #000000000000 gray(0,0,0)
    # 6709: (    0,    0,    0) #000000000000 gray(0)
    # 16680: (  128,  128,  128) #008000800080 gray(0.195315%)
    # 25206: (  256,  256,  256) #010001000100 gray(0.390631%)
    #
    # Note that the last list of numbers in the parenthesis can have either 3 or 1 value.
    RE_LINE_HISTO = r"^\s+(?P<count>\d+):\s*\(\s*(?P<rval>\d+)\s*,\s*(?P<gval>\d+)\s*,\s*(?P<bval>\d+)\s*\)\s*#(?P<hexval>[0-9A-F]{12})\s*(?P<colname>[a-zA-Z]+)\s*\((?:(?P<rperc>\d+(?:\.\d+)?)%?,(?P<gperc>\d+(?:\.\d+)?)%?,(?P<bperc>\d+(?:\.\d+)?)%?|(?P<gray>\d+(?:\.\d+)?)%?)\)"
    def __init__(self):
        if not checkProgram('identify'):
            raise Exception('[Error] ImageMagick is missing')

        # reset internal data
        self.Data = {}

    # adapt the tag name to conform with the XML format
    def normalizeName(self, name):
        name = re.sub(r'\s','_',name)
        name = re.sub(r'[^a-zA-Z0-9]','_',name)
        # trim any underscores at the end
        name = re.sub(r'_+$','',name)
        # and at the beginning
        name = re.sub(r'^_+','',name)

        def upperCallback(x):
            return x.group(1).upper()

        # apply regex to convert to camelCase
        # assuming it's in underscore format
        ccName = re.sub(r'_(.)',upperCallback,name)
        return ccName

    def runCmd(self, cmd):
        return os.popen(cmd).read()

    def parseLineGeneric(self, line):
        matchGeneric = re.match(self.RE_LINE_GENERIC, line, re.UNICODE)
        if not matchGeneric:
            return None

        # extract the values picked up by the regex above
        d = matchGeneric.groupdict()
        name = d['name']
        value = d['value']
        leading = d['leading']

        # clean up leading and trailing whitespace
        value = re.sub(r'^\s+','',value)
        value = re.sub(r'\s+$','',value)

        # get the current level using the indentation
        lc = len(leading)/2
        # everything is shifted one level deeper, because we have a root node
        lc += 1

        # a new node is created to store the information extracted
        new_node = {
                'name': name, \
                'value': value, \
                'children': [], \
                'level': lc, \
                'parent': None,\
                }
        return new_node

    def parseLineHisto(self, line, level):
        matchHisto = re.match(self.RE_LINE_HISTO, line, re.UNICODE)
        if not matchHisto:
            return None
        d = matchHisto.groupdict()
        new_node = d
        new_node['name'] = self.HISTOGRAM_ELEM
        new_node['value'] = ''
        new_node['children'] = []
        new_node['level'] = level
        new_node['parent'] = None
        return new_node

    def parse(self, filePath):
        self.parseRaw(filePath)
        self.treeTransformGroup()

    def parseRaw(self, filePath):
        # get identify output
        output = self.runCmd('identify -verbose ' + filePath)
        output = output.decode('iso-8859-1').encode('utf-8')
        lines = output.split('\n')

        #### First pass: building the AST
        # initialize the stack with a root node
        stack = 200 * [None]
        root = {'children': [], 'parent': None, 'name': '', 'value': ''}
        stack[0] = root

        # flag that indicates the histogram parsing mode is on
        hm = False
        # initialize current level, previous level, histogram parsing level
        lc,lp,lh = 1,0,0

        for line in lines:
            newNode = None

            if hm:
                newNode = self.parseLineHisto(line, lh)
                # we failed parsing the histogram line, assume we're back to generic lines
                # the current line needs to be reparsed as a generic line
                if not newNode:
                    hm = False
                    newNode = self.parseLineGeneric(line)
            else:
                newNode = self.parseLineGeneric(line)
                if newNode and 'name' in newNode and newNode['name'] == 'Histogram':
                    # if we encounter the histogram node, we turn on histogram parsing mode
                    hm = True
                    # and we store the level for all the upcoming histogram lines that follow
                    lh = 1 + newNode['level']

            if newNode:
                lc = newNode['level']

                # dispose of the 'level' attribute, we only need that information here
                if 'level' in newNode:
                    lc = newNode['level']
                    del newNode['level']

                # set parent
                newNode['parent'] = stack[lc-1]
                # add the node as a child of its immediate parent
                (stack[lc-1])['children'].append(newNode)
                # put the node on the stack, this will subsequently be used
                # by the the next iterations of this loop(if this node has children)
                stack[lc] = newNode
                # update the previous level
                lp = lc

        # store the tree in the class attribute for later use
        self.Data = root

    ## Group multiple options with the same 
    ## prefix before the colon into a new parent
    ## having the common prefix as the name
    ##
    ## an example to illustrate this:
    ## <x>
    ##   <p:a></p:a>
    ##   <p:b></p:b>
    ## </x>
    ##
    ## =>
    ##
    ## <x>
    ##   <p>
    ##      <a></a>
    ##      <b></b>
    ##   </p>
    ## </x>
    def treeTransformGroup(self):
        root = self.Data
        stack = []
        stack.append(root)

        while len(stack) > 0:
            # We're visiting the next immediate node on the stack
            # (regular DFS traversal)
            x = stack.pop()
            if 'children' in x:
                y = x['children']
                i = 0
                ## a's keys will be common prefixes
                ## and the values will be new parent nodes which hold the
                ## children that will be transfered from x
                a = {}
                while i < len(y):
                    z = y[i]
                    match = re.match(self.RE_GROUPED_ENTRY, z.get('name',''), re.UNICODE)
                    if match:
                        d = match.groupdict()
                        dPrefix = d['prefix']
                        dName = d['name']
                        if dName == '':
                            del y[i]
                            continue
                        p = a.setdefault(dPrefix, \
                                {
                                    'children': [],
                                    'name': dPrefix,
                                    'value': '',
                                    'parent': x,
                                })
                        # update z's parent because it has been moved.
                        # to illustrate this, here is how the hierarchy changes:
                        # x->z => x->p->z
                        # so p is the new parent of x
                        z['parent'] = p
                        z['name'] = dName
                        # add z to p's children
                        p['children'] += [z]
                        del y[i]
                    else:
                        i += 1
                # at this point, depending on whether there were children to be grouped
                # they were(into a), and those that couldn't be will have stayed the same(in x['children']).
                # at this point, some nodes (some children of x) have been displaced and are now 
                # children of new parent nodes, we are now expressing the fact that x has, among other children,
                # these new parent children that were created
                x['children'] += a.values()

            # Get x's children and put them on the stack
            # (continue the regular DFS)
            if 'children' in x:
                stack += x['children']

    # Note: this is only to be used for JSON serialization
    def treeTransformCompact(self, x):
        # this is basically a rebuilding of the tree in a more compact form.
        # here is a summary of what this method does:
        #
        # 1) we transform the tree as follows:
        # we aim to replace the 'children' attribute with either
        # an array or a dictionary, as follows:
        #
        # - all children have different names => we can store them in a dict
        # - if at least two children have the same name => we need to store them in an array
        #
        # 2) if a node has no children, and it has no additional attributes, then it
        # can be expressed as {k: v}
        #
        # check if it has no children
        xHasNoChildren = ('children' not in x) or ('children' in x and len(x['children']) == 0)
        # check if it has only basic properties: name,value,parent,children
        xHasOnlyNameValue = ('name' in x and 'value' in x and len(x.keys()) <= 4)

        # strip tree of parent attributes in order to avoid
        # circular references when serializing
        # to json
        del x['parent']

        if xHasNoChildren:
            del x['children']

            # x has no children
            if xHasOnlyNameValue:
                k = x['name']
                v = x['value']
                return [1,k,v]
            else:
                xname = x['name']
                xvalue = x['value']
                del x['name']
                del x['value']
                x['_value'] = xvalue
                return [2,xname,x]
        else:
            c = []
            xname = x['name']
            xvalue = x['value']
            # has children, recurse into children
            i = 0
            while i < len(x['children']):
                yi = x['children'][i]
                zi = self.treeTransformCompact(yi)
                c.append(zi)
                i += 1

            # constructing the new node
            w = None
            cnames = map(lambda z: z[1], c)
            if len(set(cnames)) == len(cnames):
                # the children all have distinct names, so w will be a dict
                w = {}
                for z in c:
                    w[z[1]] = z[2]
            else:
                # name collision are present, we need an array
                w = []
                for z in c:
                    w.append({z[1]: z[2]})

            return [3,xname,w]

    def serializeXML(self,root,xmlRoot):
        name = root['name']
        value = root['value']
        name = self.normalizeName(name)
        name = name.decode('utf-8')
        value = value.decode('utf-8')

        # serialize the node
        if 'children' in root and len(root['children']) > 0:
            for c in root['children']:
                cName = self.normalizeName(c['name'])
                xmlChild = SubElement(xmlRoot,cName)
                self.serializeXML(c,xmlChild)
        else:
            if name == self.HISTOGRAM_ELEM:
                xmlRoot.set('n', root['count'])
                xmlRoot.tag = self.HISTOGRAM_ELEM
                for k,v in root.iteritems():
                    # guard against undefined values(these are coming from the captures
                    # in the RE_LINE_HISTO regex, and the xml module will throw exceptions on
                    # the undefined values, so we want to avoid that)
                    # and check that the key is not an internal data key
                    if v and k not in ['name','value','parent','children']:
                        xmlRoot.set(k,v)
            else:
                xmlRoot.text=value

    def serializeIRODS(self,root,props,parent):
        name = root['name']
        name = self.normalizeName(name)
        name = name.decode('utf-8')
        if parent:
        	name = parent+"."+name
        value = root['value']
        value = value.decode('utf-8')
        ret = props
		
        # serialize the property
        if 'children' in root and len(root['children']) > 0:
            for c in root['children']:
               	ret += self.serializeIRODS(c,props,name)
        else:
            if root['name'] == self.HISTOGRAM_ELEM:
            	# don't serialize histogram
            	pass
            else:
                ret += "%"+name+"="+value
        return ret

    def toIRODS(self):
        Data = self.Data.copy()
        root = Data['children'][0]
        props = self.serializeIRODS(root,"",None) 
        return props[1:] # drop the first % character

    def toJSON(self):
        Data = self.Data.copy()
        # run transformation to compact tree
        Data = self.treeTransformCompact(Data)
        # serialize to json
        return json.dumps(Data[2], indent=2)

    def toXML(self):
        Data = self.Data.copy()
        root = Data['children'][0]
        # serialize the root node and return it
        tree = ElementTree(Element('Image'))
        tree.getroot().set('file',Data['children'][0]['value'])
        self.serializeXML(root, tree.getroot())
        # prettify XML and return it
        unformattedXML = tostring(tree.getroot(),encoding='utf8')
        reparsedXML = etree.fromstring(unformattedXML)
        return etree.tostring(reparsedXML, pretty_print = True)

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='ImageMagick identify -verbose parser and convertor')
    parser.add_argument("filename", help="The input file")
    parser.add_argument('--type' , '-t',default='json', help='The type of output. Can be json|irods|raw|xml.')
    args = parser.parse_args()
    
    o = ImageMagickIdentifyParser()
    o.parse(args.filename)

    if args.type == 'json':
        print o.toJSON()
    elif args.type == 'irods':
        print o.toIRODS()
    elif args.type == 'irods':
        print o.Data
    elif args.type == 'xml':
        print o.toXML()
    else:
        print "Invalid type specified:" + args.type
	   