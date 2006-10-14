#
#	THIS FILE IS PART OF THE JOKOSHER PROJECT AND LICENSED UNDER THE GPL. SEE
#	THE 'COPYING' FILE FOR DETAILS
#
#	Project.py
#	
#	This module is the central non-gui class for Jokosher. It saves and loads
#	project files, and handles any project wide functionality including;
#	settings, instruments, recording, playing, exporint, zooming, scrolling, 
#	undo, redo, volume, etc.
#
#-------------------------------------------------------------------------------

import pygst
pygst.require("0.10")
import gst
import os
import gzip
import urlparse

import TransportManager
from CommandManager import *
import Globals
import xml.dom.minidom as xml
from Instrument import *
from Monitored import *
from Utils import *
from AlsaDevices import *

#=========================================================================

#_____________________________________________________________________

def CreateNew(projecturi, name, author):
	""" Creates a new project.

		projecturi
			The filesystem location for the new project. Currently,
			only file:// URIs are considered valid.
		name
			The name of the project.
		author
			The name of the project's author.
	"""
	if name == "" or author == "" or projecturi == "":
		raise CreateProjectError(4)

	(scheme, domain,folder, params, query, fragment) = urlparse.urlparse(projecturi, "file")

	if scheme!="file":
		# raise "The URI scheme used is invalid." message
		raise CreateProjectError(5)

	filename = (name + ".jokosher")
	projectdir = os.path.join(folder, name)

	try:
		project = Project()
	except Exception, e:
		Globals.debug("Could not make project object:", e)
		raise CreateProjectError(1)

	project.name = name
	project.author = author
	project.projectfile = os.path.join(projectdir, filename)

	if os.path.exists(projectdir):
		raise CreateProjectError(2)
	else: 
		audio_dir = os.path.join(projectdir, "audio")
		try:
			os.mkdir(projectdir)
			os.mkdir(audio_dir)
		except:
			raise CreateProjectError(3)

	project.saveProjectFile(project.projectfile)

	return project

#_____________________________________________________________________

def LoadFromFile(uri):
	""" Loads a project from a save file on disk.

		uri
			The filesystem location of the project file to load. 
			Currently only file:// URIs are considered valid.
	"""
	p = Project()

	(scheme, domain, projectfile, params, query, fragment) = urlparse.urlparse(uri, "file")
	if scheme != "file":
		# raise "The URI scheme used is invalid." message
		raise OpenProjectError(1,scheme)

	Globals.debug(projectfile)

	if not os.path.exists(projectfile):
		raise OpenProjectError(4,projectfile)

	try:
		gzipfile = gzip.GzipFile(projectfile, "r")
		doc = xml.parse(gzipfile)
	except Exception, e:
		Globals.debug(e.__class__, e)
		# raise "This file doesn't unzip" message
		raise OpenProjectError(2,projectfile)
	
	p.projectfile = projectfile
	
	#only open projects with the proper version number
	version = doc.firstChild.getAttribute("version")
	if version != Globals.VERSION:
		# raise a "this project was created in a different version of Jokosher" message
		if not version: version = "0.1" # 0.1 projects had version as element, not attr
		raise OpenProjectError(3,version)
	
	params = doc.getElementsByTagName("Parameters")[0]
	
	LoadParametersFromXML(p, params)
	
	# Hack to set the transport mode
	p.transport.SetMode(p.transportMode)
	
	try:
		undo = doc.getElementsByTagName("Undo")[0]
	except IndexError:
		Globals.debug("No saved undo in project file")
	else:
		for n in undo.childNodes:
			if n.nodeType == xml.Node.ELEMENT_NODE:
				cmdList = []
				cmdList.append(str(n.getAttribute("object")))
				cmdList.append(str(n.getAttribute("function")))
				cmdList.extend(LoadListFromXML(n))
				p.savedUndoStack.append(cmdList)
	
	try:
		redo = doc.getElementsByTagName("Redo")[0]
	except IndexError:
		Globals.debug("No saved redo in project file")
	else:
		for n in redo.childNodes:
			if n.nodeType == xml.Node.ELEMENT_NODE:
				cmdList = []
				cmdList.append(str(n.getAttribute("object")))
				cmdList.append(str(n.getAttribute("function")))
				cmdList.extend(LoadListFromXML(n))
				p.savedUndoStack.append(cmdList)
	
	for instr in doc.getElementsByTagName("Instrument"):
		try:
			id = int(instr.getAttribute("id"))
		except ValueError:
			id = None
		i = Instrument(p, None, None, None, id)
		i.LoadFromXML(instr)
		p.instruments.append(i)
		if i.isSolo:
			p.soloInstrCount += 1
	
	for instr in doc.getElementsByTagName("DeadInstrument"):
		try:
			id = int(instr.getAttribute("id"))
		except ValueError:
			id = None
		i = Instrument(p, None, None, None, id)
		i.LoadFromXML(instr)
		p.graveyard.append(i)

	return p

#_____________________________________________________________________	

#=========================================================================

class Project(Monitored, CommandManaged):
	
	""" This class maintains all of the information required about single
		project.
	"""
	
	Globals.VERSION = "0.2"	# The project structure version. Will be useful for handling old save files

	#_____________________________________________________________________

	def __init__(self):
		global GlobalProjectObject
		
		Monitored.__init__(self)
		
		# set up some important lists and dictionaries:
		self.___id_list = []
		self.instruments = []
		
		self.author = "<none>"
		self.name = "<no project loaded>"
		
		# the name of the project file, complete with path
		self.projectfile = ""
		
		# View scale as pixels per second
		self.viewScale = 25.
		
		# View offset in seconds
		self.viewStart= 0.
		
		#number of solo instruments (to know if others must be muted)
		self.soloInstrCount = 0
		
		# The place where deleted instruments go
		self.graveyard = []
		
		# WARNING: any paths in this list will be deleted on exit!
		self.deleteOnCloseAudioFiles = []
		
		#The list containing the events to cut/copy
		self.clipboardList = None
		
		#This is to indicate that something which is not 
		#on the undo/redo stack needs to be saved
		self.unsavedChanges = False
		
		# Storage for the undo/redo states
		self.undoStack = []
		self.redoStack = []
		self.savedUndoStack = []
		self.savedRedoStack = []
		self.performingUndo = False
		self.performingRedo = False
		self.savedUndo = False

		self.IsPlaying = False
		#wheather we are in the process of exporting or not
		self.IsExporting = False
		
		self.RedrawTimeLine = False

		self.mainpipeline = gst.Pipeline("timeline")
		Globals.debug("created pipeline (project)")
		
		self.playbackbin = gst.Bin("playbackbin")
		
		self.adder = gst.element_factory_make("adder")
		self.playbackbin.add(self.adder)
		Globals.debug("added adder (project)")

		self.mastervolume = 0.5
		
		self.volume = gst.element_factory_make("volume")
		self.playbackbin.add(self.volume)
		Globals.debug("added volume (project)")
		
		self.adder.link(self.volume)
		
		self.masterlevel = 0.0
		self.level = gst.element_factory_make("level", "MasterLevel")
		self.level.set_property("interval", gst.SECOND / 50)
		self.level.set_property("message", True)
		self.playbackbin.add(self.level)
		Globals.debug("added master level (project)")

		#Restrict adder's output caps due to adder bug
		self.levelcaps = gst.element_factory_make("capsfilter", "levelcaps")
		caps = gst.caps_from_string("audio/x-raw-int,rate=44100,channels=2,width=16,depth=16,signed=(boolean)true")
		self.levelcaps.set_property("caps", caps)
		self.playbackbin.add(self.levelcaps)
		
		self.volume.link(self.levelcaps)
		self.levelcaps.link(self.level)
		
		self.out = gst.element_factory_make("alsasink")
		
		usedefault = False
		try:
			outdevice = Globals.settings.playback["devicecardnum"]
		except:
			usedefault = True
		if outdevice == "value":
			usedefault = True

		if usedefault:
			try:
				# Select first output device as default to avoid a GStreamer bug which causes
		                # large amounts of latency with the ALSA 'default' device.
				outdevice = GetAlsaList("playback").values()[1]
			except:
				outdevice = "default"
		Globals.debug("Output device: %s"%outdevice)
		self.out.set_property("device", outdevice)


		self.playbackbin.add(self.out)
		Globals.debug("added alsasink (project)")

		self.level.link(self.out)
		self.bus = self.mainpipeline.get_bus()
		self.bus.add_signal_watch()
		self.Mhandler = self.bus.connect("message", self.bus_message)
		self.EOShandler = self.bus.connect("message::eos", self.stop)
		self.Errorhandler = self.bus.connect("message::error", self.bus_error)
		
		self.mainpipeline.add(self.playbackbin)
		
		self.transportMode = TransportManager.TransportManager.MODE_BARS_BEATS
		self.transport = TransportManager.TransportManager(self.transportMode, self.mainpipeline)

		# [DEBUG]
		# This debug block will be removed when we release. If you see this in a release version, we
		# obviously suck. Please email us and tell us about how shit we are.

		try:
			if os.environ['JOKOSHER_DEBUG']:
				import JokDebug
				self.debug = JokDebug.JokDebug()
		except:
			pass
		# [/DEBUG]

	#_____________________________________________________________________
		
	def record(self):
		'''Start all selected instruments recording'''

		#Add all instruments to the pipeline
		numInstruments = 0
		for instr in self.instruments:
			if instr.isArmed:
				instr.record()
				numInstruments += 1

		#Check mixer states for all devices
		recMixers = []
		for device in GetAlsaList('capture').values():
			if device != 'default':
				recMixers += GetRecordingMixers(device)

		#Make sure the number of recording mixers corresponds to the number of recording instruments
		#This will fail if a sound card doesn't support multiple simultanious inputs and is being told to
		#record multiple instruments at the same time.
		if len(recMixers)==0:
			Globals.debug("No channels capable of recording found")
			raise AudioInputsError(0)

		if len(recMixers) < numInstruments:
			Globals.debug("%s %s"%(len(recMixers),numInstruments))
			raise AudioInputsError(1)

		
		#Make sure we start playing from the beginning
		self.transport.Stop()
		
		self.state_id = self.bus.connect("message::state-changed", self.state_changed)
		self.mainpipeline.set_state(gst.STATE_PAUSED)

		# [DEBUG]
		# This debug block will be removed when we release. If you see this in a release version, we
		# obviously suck. Please email us and tell us about how shit we are.
		try:
			if os.environ['JOKOSHER_DEBUG']:
				Globals.debug("Play Pipeline:")
				self.debug.ShowPipelineTree(self.mainpipeline)
		except:
			pass
		# [/DEBUG]
				
	#_____________________________________________________________________

	def state_changed(self, bus, message, movePlayhead=True):
		""" Handler for GStreamer statechange events. 
		"""
		Globals.debug("STATE CHANGED")
		old, new, pending = self.mainpipeline.get_state(0)
		#Move forward to playing when we reach paused (check pending to make sure this is the final destination)
		if new == gst.STATE_PAUSED and pending == gst.STATE_VOID_PENDING and not self.IsPlaying:
			bus.disconnect(self.state_id)
			#The transport manager will seek if necessary, and then set the pipeline to STATE_PLAYING
			self.transport.Play(movePlayhead)
			self.IsPlaying = True

	#_____________________________________________________________________
				
	def play(self, movePlayhead = True):
		'''Set all instruments playing'''
		
		if len(self.instruments) > 0:
			Globals.debug("play() in Project.py")

			for ins in self.instruments:
				if ins.effects:
					Globals.debug("there are effects")
					Globals.debug("pipeline is NULL or ready, gonna prepare effects bin")
					ins.PrepareEffectsBin()
					ins.converterElement.link(ins.effectsbin)
					ins.effectsbin.link(ins.volumeElement)
				else:
					Globals.debug("there are no effects")
					try:
						ins.converterElement.link(ins.volumeElement)
					except:
						pass

			# And set it going
			self.state_id = self.bus.connect("message::state-changed", self.state_changed, movePlayhead)
			#set to PAUSED so the transport manager can seek first (if needed)
			#the pipeline will be set to PLAY by self.state_changed()
			self.mainpipeline.set_state(gst.STATE_PAUSED)
			
			Globals.debug("just set state to PLAYING")

			# [DEBUG]
			# This debug block will be removed when we release. If you see this in a release version, we
			# obviously suck. Please email us and tell us about how shit we are.
			try:
				if os.environ['JOKOSHER_DEBUG']:
					Globals.debug("Play Pipeline:")
					self.debug.ShowPipelineTree(self.mainpipeline)
			except:
				pass
			# [/DEBUG]

	#_____________________________________________________________________

	def bus_message(self, bus, message):
		""" Handler for GStreamer bus messages.
		"""
		st = message.structure
		
		if st and st.get_name() == "level" and not message.src is self.level:
			for instr in self.instruments:
				if message.src is instr.levelElement:
					instr.SetLevel(DbToFloat(st["decay"][0]))
					break
				
		if st and st.get_name() == "level" and message.src is self.level:
			self.SetLevel(DbToFloat(st["decay"][0]))
			
		return True

	#_____________________________________________________________________


	def bus_error(self, bus, message):
		""" Handler for GStreamer error messages.
		"""
		st = message.structure
		error, debug = message.parse_error()
		
		introstring = "Argh! Something went wrong and a serious error occurred:"
		outrostring = "It is recommended that you report this to the Jokosher developers or get help at http://www.jokosher.org/forums/"
		outputtext = introstring + "\n\n" + str(error) + "\n\n" + str(debug) + "\n\n" + outrostring
		
		dlg = gtk.MessageDialog(None,
			0,
			gtk.MESSAGE_ERROR,
			gtk.BUTTONS_CANCEL,
			outputtext)
		dlg.connect('response', lambda dlg, response: dlg.destroy())
		dlg.show()
		
		Globals.debug(outputtext)

	#_____________________________________________________________________
				
	def newPad(self, element, pad, instrument):
		""" Creates a new GStreamer pad on the specified instrument.

			TODO - This looks like it should be refactored into the 
					Instrument class. JasonF.
		"""
		Globals.debug("NEW PAD")
		convpad = instrument.converterElement.get_compatible_pad(pad, pad.get_caps())
		pad.link(convpad)

	#_____________________________________________________________________

	def removePad(self, element, pad, instrument):
		""" Removes a new GStreamer pad from the specified instrument.

			TODO - This looks like it should be refactored into the 
					Instrument class. JasonF.
		"""
		Globals.debug("pad removed")
#		print pad
#		convpad = instrument.converterElement.get_compatible_pad(pad, pad.get_caps())
#		pad.unlink(convpad)
		instrument.composition.set_state(gst.STATE_READY)

	#_____________________________________________________________________

	#Alias to self.play
	def export(self, filename, format=None):
		'''
		   Export to location filename with format specified by format variable.
		   Format is a string of the file extension as used in Globals.EXPORT_FORMATS.
		   ie; "ogg", "mp3", "wav".
		   If no format is given, the format will be guessed by the file extension.
		'''
		#NULL is required because some elements will be destroyed when we remove the references
		self.mainpipeline.set_state(gst.STATE_NULL)
		
		if not format:
			format = filename[filename.rfind(".")+1:].lower()
		
		exportingFormatDict = None
		for formatDict in Globals.EXPORT_FORMATS:
			if format == formatDict["extension"]:
				self.IsExporting = True
				exportingFormatDict = formatDict
				break
		if not self.IsExporting:
			Globals.debug("Unknown filetype for export")
			return -1
		
		#remove and unlink the alsasink
		self.playbackbin.remove(self.out, self.level)
		self.levelcaps.unlink(self.level)
		self.level.unlink(self.out)
		
		#create filesink
		self.outfile = gst.element_factory_make("filesink", "export_file")
		self.outfile.set_property("location", filename)
		self.playbackbin.add(self.outfile)
		
		#create encoder
		self.encode = gst.element_factory_make(exportingFormatDict["encoder"])
		self.mux = None
		self.exportconvert = None
		
		self.playbackbin.add(self.encode)
		
		if exportingFormatDict["requiresAudioconvert"]:
			#audioconvert may be required because level and vorbisenc have different caps
			self.exportconvert = gst.element_factory_make("audioconvert", "export_convert")
			self.playbackbin.add(self.exportconvert)
			#link the new audioconvert
			self.levelcaps.link(self.exportconvert)
			self.exportconvert.link(self.encode)
		else:
			self.levelcaps.link(self.encode)
		
		#this is None is the format doesn't use a muxer
		if exportingFormatDict["muxer"]:
			self.mux = gst.element_factory_make(exportingFormatDict["muxer"])
			self.playbackbin.add(self.mux)
			#link the new muxer
			self.encode.link(self.mux)
			self.mux.link(self.outfile)
		else:
			self.encode.link(self.outfile)
			
		#disconnect the bus_message() which will make the transport manager progress move
		self.bus.disconnect(self.Mhandler)
		self.bus.disconnect(self.EOShandler)
		self.EOShandler = self.bus.connect("message::eos", self.export_eos)
		
		#Make sure we start playing from the beginning
		self.transport.Stop()
		#start the pipeline!
		self.play(movePlayhead=False)

	#_____________________________________________________________________
	
	def export_eos(self, bus=None, message=None):
		""" GStreamer End Of Stream handler. It is connected to eos on 
			mainpipeline while export is taking place.
		"""
		
		if not self.IsExporting:
			return
		else:
			self.IsExporting = False
	
		self.stop()
		#NULL is required because elements will be destroyed when we delete them
		self.mainpipeline.set_state(gst.STATE_NULL)
	
		self.bus.disconnect(self.EOShandler)
		self.Mhandler = self.bus.connect("message::element", self.bus_message)
		self.EOShandler = self.bus.connect("message::eos", self.stop)
		
		#remove the filesink and encoder
		self.playbackbin.remove(self.outfile, self.encode)		
		
		if self.exportconvert:
			self.playbackbin.remove(self.exportconvert)
			self.levelcaps.unlink(self.exportconvert)
		else:
			self.levelcaps.unlink(self.encode)
			
		if self.mux:
			self.playbackbin.remove(self.mux)
			
		#dispose of the elements
		del self.outfile, self.encode, self.mux, self.exportconvert
		
		#re-add all the alsa playback elements
		self.playbackbin.add(self.out, self.level)
		self.levelcaps.link(self.level)
		self.level.link(self.out)
	
	#_____________________________________________________________________
	
	def get_export_progress(self):
		""" Returns tuple with number of seconds done, and number of total 
			seconds.
		"""
		if self.IsExporting:
			try:
				total = self.mainpipeline.query_duration(gst.FORMAT_TIME)[0]
				cur = self.mainpipeline.query_position(gst.FORMAT_TIME)[0]
			except gst.QueryError:
				return (-1, -1)
			else:
				if cur > total:
					total = cur
				return (float(cur)/gst.SECOND, float(total)/gst.SECOND)
		else:
			return (100, 100)
		
	#_____________________________________________________________________
	
	def stop(self, bus=None, message=None):
		'''Stop playing or recording'''
		for instr in self.instruments:
			instr.stop()

		Globals.debug("Stop pressed, about to set state to READY")

		#read pipeline for current position - it will have been read
		#periodically in TimeLine.py but could be out by 1/FPS
		self.transport.QueryPosition()
		self.mainpipeline.set_state(gst.STATE_READY)
		self.IsPlaying = False
			
		Globals.debug("Stop pressed, state just set to READY")

		
		# [DEBUG]
		# This debug block will be removed when we release. If you see this in a release version, we
		# obviously suck. Please email us and tell us about how shit we are.
		try:
			if os.environ['JOKOSHER_DEBUG']:
				Globals.debug("PIPELINE AFTER STOP:")
				self.debug.ShowPipelineTree(self.mainpipeline)
		except:
			pass
		# [/DEBUG]
			
		self.transport.Stop()
			
	#_____________________________________________________________________

	def terminate(self):
		''' Terminate all instruments (used to disregard recording when an 
			error occurs after instruments have started).
		'''
		for instr in self.instruments:
			instr.terminate()

		Globals.debug("Terminating recording.")

		self.mainpipeline.set_state(gst.STATE_READY)
		self.IsPlaying = False

		self.transport.Stop()

	#_____________________________________________________________________
	
	def saveProjectFile(self, path=None):
		""" Saves the project and its children as an XML file 
			to the path specified by file.
		"""
		
		if not path:
			if not self.projectfile:
				raise "No save path specified!"
			path = self.projectfile
			
		if not path.endswith(".jokosher"):
			path = path + ".jokosher"
			
		#sync the transport's mode with the one which will be saved
		self.transportMode = self.transport.mode
		
		self.unsavedChanges = False
		#purge main undo stack so that it will not prompt to save on exit
		self.savedUndoStack.extend(self.undoStack)
		self.undoStack = []
		#purge savedRedoStack so that it will not prompt to save on exit
		self.redoStack.extend(self.savedRedoStack)
		self.savedRedoStack = []
		
		doc = xml.Document()
		head = doc.createElement("JokosherProject")
		doc.appendChild(head)
		
		head.setAttribute("version", Globals.VERSION)
		
		params = doc.createElement("Parameters")
		head.appendChild(params)
		
		items = ["viewScale", "viewStart", "name", "author", "transportMode"]
		
		StoreParametersToXML(self, doc, params, items)
			
		undo = doc.createElement("Undo")
		head.appendChild(undo)
		for cmd in self.savedUndoStack:
			e = doc.createElement("Command")
			e.setAttribute("object", cmd[0])
			e.setAttribute("function", cmd[1])
			undo.appendChild(e)
			StoreListToXML(doc, e, cmd[2:], "Parameter")
		
		redo = doc.createElement("Redo")
		head.appendChild(redo)
		for cmd in self.redoStack:
			e = doc.createElement("Command")
			e.setAttribute("object", cmd[0])
			e.setAttribute("function", cmd[1])
			redo.appendChild(e)
			StoreListToXML(doc, e, cmd[2:], "Parameter")
		
			
		for i in self.instruments:
			i.StoreToXML(doc, head)
			
		for i in self.graveyard:
			i.StoreToXML(doc, head, graveyard=True)
		
		try:
			#append "~" in case the saving fails
			f = gzip.GzipFile(path +"~", "w")
			f.write(doc.toprettyxml())
			f.close()
		except:
			os.remove(path + "~")
		else:
			#if the saving doesn't fail, move it to the proper location
			os.rename(path + "~", path)		
		
		self.StateChanged()
	
	#_____________________________________________________________________

	def closeProject(self):
		""" Closes down this project.
		"""
		global GlobalProjectObject
		GlobalProjectObject = None
		
		for file in self.deleteOnCloseAudioFiles:
			if os.path.exists(file):
				Globals.debug("Deleting copied audio file:", file)
				os.remove(file)
		self.deleteOnCloseAudioFiles = []
		
		self.instruments = []
		self.metadata = {}
		self.projectfile = ""
		self.projectdir = ""
		self.name = ""
		self.listeners = []
		
	#_____________________________________________________________________
	
	def Undo(self):
		""" Attempts to revert the last user action by popping an action
			from the undo stack and executing it.
		"""
		self.performingUndo = True
		
		if len(self.undoStack):
			cmd = self.undoStack.pop()
			self.ExecuteCommand(cmd)
			
		elif len(self.savedUndoStack):
			self.savedUndo = True
			cmd = self.savedUndoStack.pop()
			self.ExecuteCommand(cmd)
			self.savedUndo = False
			
		self.performingUndo = False
	
	#_____________________________________________________________________
	
	def Redo(self):
		""" Attempts to redo the last undone action.
		"""
		self.performingRedo = True
		
		if len(self.savedRedoStack):
			self.savedUndo = True
			cmd = self.savedRedoStack.pop()
			self.ExecuteCommand(cmd)
			self.savedUndo = False
			
		elif len(self.redoStack):
			cmd = self.redoStack.pop()
			self.ExecuteCommand(cmd)
			
		self.performingRedo = False

	#_____________________________________________________________________
	
	def AppendToCurrentStack(self, object):
		""" Appends the action specified by object onto the relevant
			undo/redo stack.
		"""
		if self.savedUndo and self.performingUndo:
			self.savedRedoStack.append(object)
		elif self.savedUndo and self.performingRedo:
			self.savedUndoStack.append(object)
		elif self.performingUndo:
			self.redoStack.append(object)
		elif self.performingRedo:
			self.undoStack.append(object)
		else:
			self.undoStack.append(object)
			self.redoStack = []
			#if we have undone anything that was previously saved
			if len(self.savedRedoStack):
				self.savedRedoStack = []
				#since there is no other record that something has 
				#changed after savedRedoStack is purged
				self.unsavedChanges = True
		self.StateChanged()
	
	#_____________________________________________________________________
	
	def CheckUnsavedChanges(self):
		"""Uses boolean self.unsavedChanges and Undo/Redo to 
		   determine if the program needs to save anything on exit
		"""
		return self.unsavedChanges or \
			len(self.undoStack) > 0 or \
			len(self.savedRedoStack) > 0
	
	#_____________________________________________________________________
	
	def ExecuteCommand(self, cmdList):
		""" This function executes the string cmd from the undo/redo stack.
			Commands are made up of a list of which the first two items are
			the object (and it's ID if relevant), and the function to call. 
			The 3rd, 4th, etc. items in the list are the parameters to give to
			the function when it is called.

			i.e.
				["E2", "Move", 1, 2]
				which means 'Call Move(1, 2)' on the Event with ID=2
		"""		
		obj = cmdList[0]
		target_object = None
		if obj[0] == "P":		# Check if the object is a Project
			target_object = self
		elif obj[0] == "I":		# Check if the object is an Instrument
			id = int(obj[1:])
			target_object = [x for x in self.instruments if x.id==id][0]
		elif obj[0] == "E":		# Check if the object is an Event
			id = int(obj[1:])
			for i in self.instruments:
				# First of all see if it's alive on an instrument
				n = [x for x in i.events if x.id==id]
				if not n:
					# If not, check the graveyard on each instrument
					n = [x for x in i.graveyard if x.id==id]
				if n:
					target_object = n[0]
					break
		
		getattr(target_object, cmdList[1])(*cmdList[2:])

	#_____________________________________________________________________
	
	def AddInstrument(self, name, type, pixbuf):
		''' Adds a new instrument to the project,
		   and return the ID for that instrument.
		
			undo : DeleteInstrument: temp
		'''
			
		instr = Instrument(self, name, type, pixbuf)
		audio_dir = os.path.join(os.path.split(self.projectfile)[0], "audio")
		instr.path = os.path.join(audio_dir)
		
		self.temp = instr.id
		self.instruments.append(instr)
		
		return instr.id
		
	#_____________________________________________________________________	
		
	def DeleteInstrument(self, id):
		''' Removes the instrument matching id from the project.
			id: Unique ID of the instument to remove.

			undo : ResurrectInstrument: temp
		'''
		
		instr = [x for x in self.instruments if x.id == id][0]
		
		instr.RemoveAndUnlinkPlaybackbin()
		
		self.graveyard.append(instr)
		self.instruments.remove(instr)
		if instr.isSolo:
			self.soloInstrCount -= 1
			self.OnAllInstrumentsMute()
			
		self.temp = id
	
	#_____________________________________________________________________
	
	def ResurrectInstrument(self, id):
		''' Brings a deleted Instrument back from the graveyard.

			undo : DeleteInstrument: temp
		'''
		instr = [x for x in self.graveyard if x.id == id][0]
		
		instr.AddAndLinkPlaybackbin()
		
		self.instruments.append(instr)
		if instr.isSolo:
			self.soloInstrCount += 1
			self.OnAllInstrumentsMute()
		instr.isVisible = True
		self.graveyard.remove(instr)
		self.temp = id
		
	#_____________________________________________________________________
	
	def MoveInstrument(self, id, position):
		'''	Move an instrument in the instrument list.
			Used for drag and drop ordering of instruments in
			InstrumentViewer.py
			
			undo : MoveInstrument: temp, temp1
		'''
		self.temp = id
		instr = [x for x in self.instruments if x.id == id][0]
		self.temp1 = self.instruments.index(instr)
		
		self.instruments.remove(instr)
		self.instruments.insert(position, instr)		
	
	#_____________________________________________________________________
	
	def ClearEventSelections(self):
		''' Clears the selection of any events '''
		for instr in self.instruments:
			for ev in instr.events:
				ev.SetSelected(False)

	#_____________________________________________________________________

	def SelectInstrument(self, instrument=None):
		''' Selects instrument and clears the selection of all other instruments. '''
		for instr in self.instruments:
			if instr is not instrument:
				instr.SetSelected(False)
			else:
				instr.SetSelected(True)
			
	#_____________________________________________________________________
	
	def SetViewStart(self, start):
		""" Sets the time at which the project view should start.

			start
				Start time for the view in seconds.
		"""
		if self.viewStart != start:
			self.viewStart = start
			self.RedrawTimeLine = True
			self.StateChanged()
		
	#_____________________________________________________________________
	
	def SetViewScale(self, scale):
		""" Sets the scale of the project view.
		"""
		self.viewScale = scale
		self.RedrawTimeLine = True
		self.StateChanged()
		
	#_____________________________________________________________________

	def GetProjectLength(self):
		""" Returns the length of the project in seconds.
		"""
		length = 0
		for instr in self.instruments:
			for ev in instr.events:
				size = ev.start + ev.duration
				length = max(length, size)
		return length

	#_____________________________________________________________________
	
	def OnAllInstrumentsMute(self):
		""" Mutes all Instruments in this project.
		"""
		for instr in self.instruments:
			instr.OnMute()
			
	#_____________________________________________________________________
	
	def GenerateUniqueID(self, id = None):
		""" Creates a new unique ID which can be assigned to an new 
			project object.
		"""
		if id != None:
			if id in self.___id_list:
				Globals.debug("Error: id", id, "already taken")
			else:
				self.___id_list.append(id)
				return id
				
		counter = 0
		while True:
			if not counter in self.___id_list:
				self.___id_list.append(counter)
				return counter
			counter += 1
	
	#_____________________________________________________________________

	def SetVolume(self, volume):
		"""Sets the volume of the instrument in the range 0..1
		"""
		self.mastervolume = volume
		self.volume.set_property("volume", volume)

	#_____________________________________________________________________

	def SetLevel(self, level):
		""" Note that this sets the current REPORTED level, NOT THE VOLUME!
		"""
		self.masterlevel = level

	#_____________________________________________________________________

	def ValidateProject(self):
		""" Checks that the project is valid - i.e. that of the files and 
			images that it references can be found.

			Returns
				True if the project is valid, False if not.
		"""
		unknownfiles=[]
		unknownimages=[]

		for instr in self.instruments:
			for ev in instr.events:
				if (ev.file!=None) and (not os.path.exists(ev.file)) and (not ev.file in unknownfiles):
					unknownfiles.append(ev.file)
		if len(unknownfiles)>0 or len(unknownimages)>0:
			raise InvalidProjectError(unknownfiles,unknownimages)

		return True
	
	#_____________________________________________________________________
	
	def SetTransportMode(self, val):
		"""
			Sets the Mode in the Transportmanager. Used to enable Undo/Redo.
			
			undo : SetTransportMode: temp
		"""
		self.temp = self.transport.mode
		self.transport.SetMode(val)

#=========================================================================
	
class OpenProjectError(EnvironmentError):
	def __init__(self, errno, info = None):
		""" Error Numbers:
		   1) Invalid uri passed for the project file
		   2) Unable to unzip the project
		   3) Project created by a different version of Jokosher 
		If a version string is given, it means the project file was created by
		another version of Jokosher. That version is specified in the string.
		   4) Project file doesn't exist
		"""
		EnvironmentError.__init__(self)
		self.info = info
		self.errno = errno
		
#=========================================================================

class CreateProjectError(Exception):
	def __init__(self, errno):
		"""Error numbers:
		   1) Unable to create a project object
		   2) Path for project file already exists
		   3) Unable to create file. (Invalid permissions, read-only, or the disk is full)
		   4) Invalid path, name or author
		   5) Invalid uri passed for the project file
		"""
		Exception.__init__(self)
		self.errno=errno

#=========================================================================

class AudioInputsError(Exception):
	def __init__(self, errno):
		"""Error numbers:
		   1) No recording channels found
		   2) Sound card is not capable of multiple simultanious inputs
		"""
		Exception.__init__(self)
		self.errno = errno

#=========================================================================

class InvalidProjectError(Exception):
	def __init__(self, missingfiles,missingimages):
		Exception.__init__(self)
		self.files=missingfiles
		self.images=missingimages

#=========================================================================

GlobalProjectObject = None
