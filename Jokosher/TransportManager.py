#
#	THIS FILE IS PART OF THE JOKOSHER PROJECT AND LICENSED UNDER THE GPL. SEE
#	THE 'COPYING' FILE FOR DETAILS
#
#	TransportManager.py
#	
#	This class handles the current cursor position and the gstreamer
#	bits for rewinding, fast forwarding and seeking.
#	
#
#-------------------------------------------------------------------------------

import pygst
pygst.require("0.10")
import gst
import gobject
from Monitored import *

#=========================================================================

class TransportManager(Monitored):
	"""
	This class handles the current cursor position and the gstreamer
	bits for rewinding, fast forwarding and seeking.
	"""

	""" Position update rate in Frames Per Second. """
	FPS = 30.
	
	""" Timing resolution - number of ticks ber beat. """
	TICKS_PER_BEAT = 256
	
	""" How many times normal speed the position moves when seeking. """
	SEEK_RATE = 5.
	
	""" Display mode in hours, minutes and seconds. """
	MODE_HOURS_MINS_SECS = 1
	
	""" Display mode in bars, beats and ticks. """
	MODE_BARS_BEATS = 2

	#_____________________________________________________________________

	def __init__(self, initialMode, project):
		"""
		Creates a new instance of TransportManager.
		
		Parameters:
			initialMode --the initial mode for the timeline display.
						Possible values:
						MODE_HOURS_MINS_SECS
						MODE_BARS_BEATS
			project -- reference to the current Project.
		"""
		Monitored.__init__(self)
		
		self.project = project
		self.pipeline = self.project.mainpipeline
		self.position = 0
		self.PrevPosition = 0	
		self.isPlaying = False
		self.isPaused = False
		self.isReversing = False
		self.isForwarding = False
		self.UpdateTimeout = False
		
		self.mode = initialMode

	#_____________________________________________________________________
	
	def Play(self, newAudioState):
		"""
		Called when play button has been pressed (or whilst exporting
		in which case, newAudioState will be set to AUDIO_EXPORTING).
		
		Parameters:
			newAudioState -- new audio state to set the Project to.
		"""
		#the state must be set to paused before playing
		if self.pipeline.get_state(0)[1] != gst.STATE_PAUSED:
			return
			
		self.isPlaying = True
		self.project.SetAudioState(newAudioState)
		
		if self.position > 0:
			self.SeekTo(self.position)
		
		self.pipeline.set_state(gst.STATE_PLAYING)
		#for normal playback then we need to start the timeout that will 
		#control the movement of the playhead
		if not self.project.GetIsExporting():
			self.StartUpdateTimeout()
		
	#_____________________________________________________________________
		
	def Pause(self):
		"""
		Pause the playback.
		"""
		self.isPlaying = False
		self.isPaused = True
		self.project.SetAudioState(self.project.AUDIO_PAUSED)
		self.pipeline.set_state(gst.STATE_PAUSED)
	
	#_____________________________________________________________________
		
	def Stop(self):
		"""
		Stops the playback.
		"""
		self.isPlaying = False
		self.project.SetAudioState(self.project.AUDIO_STOPPED)
		self.SetPosition(0.0, True)
		self.pipeline.set_state(gst.STATE_READY)
		
	#_____________________________________________________________________
		
	def Reverse(self, turnOn):
		"""
		Called when rewind button is
			a) pressed - turnOn = True
			b) released - turnOn = False
			
		Parameters:
			turnOn -- state of the rewind button.
		"""
		if self.isReversing == turnOn:
			#there is no change in reversing state
			return
		
		self.isReversing = turnOn
		if turnOn:
			if self.isPlaying:
				#Pause playback while seeking
				self.pipeline.set_state(gst.STATE_PAUSED)
			self.StartUpdateTimeout()
		else:
			self.SeekTo(self.GetPosition())
			if self.isPlaying:
				#resume playback if it was playing before
				self.pipeline.set_state(gst.STATE_PLAYING)
		
	#_____________________________________________________________________
		
	def Forward(self, turnOn):
		"""
		Called when fast forward button is
			a) pressed - turnOn = True
			b) released - turnOn = False
			
		Parameters:
			turnOn -- state of the fast forward button.
		"""
		if self.isForwarding == turnOn:
			#there is no change in the forwarding state
			return
	
		self.isForwarding = turnOn
		if turnOn:
			if self.isPlaying:
				#Pause playback while seeking
				self.pipeline.set_state(gst.STATE_PAUSED)
			self.StartUpdateTimeout()
		else:
			self.SeekTo(self.GetPosition())
			if self.isPlaying:
				#resume playback if it was playing before
				self.pipeline.set_state(gst.STATE_PLAYING)
		
	#_____________________________________________________________________
	
	def GetPosition(self):
		"""
		Obtain the current playhead position.
		
		Returns:
			the current playhead cursor position.
		"""
		return self.position
	
	#_____________________________________________________________________
	
	def SetPosition(self, pos, stopAction=False):
		"""
		Change the current position variable.
		
		Considerations:
			Calls StateChanged to trigger response on all classes
			that are listening to this object.
		
		Parameters:
			pos -- new playhead cursor position.
			stopAction -- true if this position change was a result of stopping
					and sending the position back to zero.
		"""
		if self.position != pos:
			self.PrevPosition = self.position
			self.position = pos
			if stopAction:
				self.StateChanged("position", "stop-action")
			else:
				self.StateChanged("position")

	#_____________________________________________________________________

	def SetMode(self, mode):
		"""
		In most cases, for undo compatibility, use Project.SetTransportMode().
		
		Parameters:
			mode -- new transport mode to be set.
		"""
		if self.mode != mode:
			self.mode = mode
			self.StateChanged("transport-mode", mode)
		
	#_____________________________________________________________________
	
	def GetPositionAsBarsAndBeats(self):
		"""
		Obtain the current position in bars, beats and ticks.
		
		Returns:
			tuple of the current position as (bar, beats, ticks).
		"""
		mins = self.position / 60.
		beats = int(mins * self.project.bpm)
		ticks = ((mins - (beats / float(self.project.bpm))) * self.project.bpm) * self.TICKS_PER_BEAT
		bars = int(beats / self.project.meter_nom)
		beats -= bars * self.project.meter_nom
		return (bars+1, beats+1, ticks)
		
	#_____________________________________________________________________
	
	def GetPositionAsHoursMinutesSeconds(self):
		"""
		Obtain the current position in hours, minutes and seconds.
		
		Returns:
			tuple of the current position as (hours, minutes, seconds, milliseconds).
		"""
		hours = int(self.position / 3600)
		mins = int((self.position % 3600) / 60)
		secs = int(self.position % 60)
		millis = int((self.position * 1000) % 1000)
		
		return (hours, mins, secs, millis)
	
	#_____________________________________________________________________

	def StartUpdateTimeout(self):
		"""
		Starts the timeout that will control the playhead display.
		"""
		if not self.UpdateTimeout:
			gobject.timeout_add(int(1000/self.FPS), self.OnUpdate)
			self.UpdateTimeout = True
	
	#_____________________________________________________________________
	
	def OnUpdate(self):
		"""
		The timeout callback - called every 1/FPS to move the 
		playhead display on.
		
		Returns:
			True -- pipeline is playing, keep calling this method.
			False -- pipeline is paused or stopped, stop calling this method.
		"""
		if self.isReversing:
			newpos = self.position - self.SEEK_RATE/self.FPS
			self.SetPosition(max(newpos, 0))
		elif self.isForwarding:
			self.SetPosition(self.position + self.SEEK_RATE/self.FPS)
		elif self.isPlaying:
			try:
				#if pipeline should be playing and has not quite 
				#yet started then ignore this time through
				if self.pipeline.get_state(0)[1] == gst.STATE_PAUSED:
					return True
				self.QueryPosition()
			except gst.QueryError:
				pass
		else:
			self.UpdateTimeout = False
			#Prevent the timeout from calling us again
			return False
			
		#Make sure the timeout calls us again
		return True
		
	#_____________________________________________________________________
	
	def SeekTo(self, pos):
		"""
		Performs a pipeline seek to alter position of the playhead cursor.
		
		Parameters:
			pos -- position to place the playhead cursor.
		"""
		#make sure we cant seek to before the beginning
		pos = max(0, pos)
		if self.isPlaying or self.isPaused:
			self.pipeline.seek( 1.0, gst.FORMAT_TIME, gst.SEEK_FLAG_FLUSH,
					gst.SEEK_TYPE_SET, long(pos * gst.SECOND), 
					gst.SEEK_TYPE_NONE, -1)
		self.SetPosition(pos)
		
	#_____________________________________________________________________
	
	def QueryPosition(self):
		"""
		Reads the current playhead cursor position by querying pipeline.
		"""
		pos = self.pipeline.query_position(gst.FORMAT_TIME)[0]
		self.SetPosition(float(pos) / gst.SECOND)
		
	#_____________________________________________________________________
	
#=========================================================================
