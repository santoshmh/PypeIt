""" Implements KCWI-specific functions.
"""

import pdb
from matplotlib import pyplot as plt
import glob
import numpy as np

from astropy.io import fits

from pypeit import msgs
from pypeit import telescopes
from pypeit.core import parse
from pypeit.core import framematch
from pypeit.par import pypeitpar
from pypeit.spectrographs import spectrograph


class KeckKCWISpectrograph(spectrograph.Spectrograph):
    """
    Child to handle Keck/KCWI specific code
    """

    def __init__(self):
        # Get it started
        super(KeckKCWISpectrograph, self).__init__()
        self.spectrograph = 'keck_kcwi_base'
        self.telescope = telescopes.KeckTelescopePar()

    def config_specific_par(self, scifile, inp_par=None):
        """
        Modify the PypeIt parameters to hard-wired values used for
        specific instrument configurations.

        .. todo::
            Document the changes made!

        Args:
            scifile (str):
                File to use when determining the configuration and how
                to adjust the input parameters.
            inp_par (:class:`pypeit.par.parset.ParSet`, optional):
                Parameter set used for the full run of PypeIt.  If None,
                use :func:`default_pypeit_par`.

        Returns:
            :class:`pypeit.par.parset.ParSet`: The PypeIt paramter set
            adjusted for configuration specific parameter values.
        """
        par = self.default_pypeit_par() if inp_par is None else inp_par

        return par

    def init_meta(self):
        """
        Generate the meta data dict
        Note that the children can add to this

        Returns:
            self.meta: dict (generated in place)

        """
        meta = {}
        # Required (core)
        meta['ra'] = dict(ext=0, card='RA')
        meta['dec'] = dict(ext=0, card='DEC')
        meta['target'] = dict(ext=0, card='TARGNAME')
        meta['dispname'] = dict(ext=0, card='BGRATNAM')
        meta['decker'] = dict(ext=0, card='IFUNAM')
        meta['binning'] = dict(card=None, compound=True)

        meta['mjd'] = dict(ext=0, card='MJD')
        meta['exptime'] = dict(ext=0, card='ELAPTIME')
        meta['airmass'] = dict(ext=0, card='AIRMASS')

        # Extras for config and frametyping
        meta['hatch'] = dict(ext=0, card='HATNUM')
        meta['calpos'] = dict(ext=0, card='CALXPOS')
        meta['dispangle'] = dict(ext=0, card='BGRANGLE', rtol=0.01)

        # Lamps
        lamp_names = ['LMP0', 'LMP1', 'LMP2', 'LMP3']  # FeAr, ThAr, Aux, Continuum
        for kk, lamp_name in enumerate(lamp_names):
            meta['lampstat{:02d}'.format(kk + 1)] = dict(ext=0, card=lamp_name+'STAT')
        for kk, lamp_name in enumerate(lamp_names):
            if lamp_name == 'LMP3':
                # There is no shutter on LMP3
                meta['lampshst{:02d}'.format(kk + 1)] = dict(ext=0, card=None, default=1)
                continue
            meta['lampshst{:02d}'.format(kk + 1)] = dict(ext=0, card=lamp_name+'SHST')
        # Ingest
        self.meta = meta

    def compound_meta(self, headarr, meta_key):
        if meta_key == 'binning':
            binspatial, binspec = parse.parse_binning(headarr[0]['BINNING'])
            binning = parse.binning2string(binspec, binspatial)
            return binning
        else:
            msgs.error("Not ready for this compound meta")

    def configuration_keys(self):
        """
        Return the metadata keys that defines a unique instrument
        configuration.

        This list is used by :class:`pypeit.metadata.PypeItMetaData` to
        identify the unique configurations among the list of frames read
        for a given reduction.

        Returns:
            list: List of keywords of data pulled from file headers and
            used to constuct the :class:`pypeit.metadata.PypeItMetaData`
            object.
        """
        return ['dispname', 'decker', 'binning', 'dispangle']

    def check_frame_type(self, ftype, fitstbl, exprng=None):
        """
        Check for frames of the provided type.
        """
        good_exp = framematch.check_frame_exptime(fitstbl['exptime'], exprng)
        if ftype == 'science':
            return good_exp & self.lamps(fitstbl, 'off') & (fitstbl['hatch'] == '1')  #hatch=1,0=open,closed
        if ftype == 'bias':
            return good_exp & self.lamps(fitstbl, 'off') & (fitstbl['hatch'] == '0')
        if ftype in ['pixelflat', 'trace']:
            # Flats and trace frames are typed together
            return good_exp & self.lamps(fitstbl, 'dome_noarc') & (fitstbl['hatch'] == '0') & (fitstbl['calpos'] == '6')
        if ftype in ['dark']:
            # Dark frames
            return good_exp & self.lamps(fitstbl, 'off') & (fitstbl['hatch'] == '0')
        if ftype in ['bar']:
            # Bar frames
            return good_exp & self.lamps(fitstbl, 'dome') & (fitstbl['hatch'] == '0') & (fitstbl['calpos'] == '4')
        if ftype in ['arc', 'tilt']:
            return good_exp & self.lamps(fitstbl, 'arcs') & (fitstbl['hatch'] == '0')
        if ftype in ['pinhole']:
            # Don't type pinhole frames
            return np.zeros(len(fitstbl), dtype=bool)

        msgs.warn('Cannot determine if frames are of type {0}.'.format(ftype))
        return np.zeros(len(fitstbl), dtype=bool)

    def lamps(self, fitstbl, status):
        """
        Check the lamp status.

        Args:
            fitstbl (:obj:`astropy.table.Table`):
                The table with the fits header meta data.
            status (:obj:`str`):
                The status to check.  Can be `off`, `arcs`, or `dome`.

        Returns:
            numpy.ndarray: A boolean array selecting fits files that
            meet the selected lamp status.

        Raises:
            ValueError:
                Raised if the status is not one of the valid options.
        """
        if status == 'off':
            # Check if all are off
            lampstat = np.array([(fitstbl[k] == '0') | (fitstbl[k] == 'None')
                                    for k in fitstbl.keys() if 'lampstat' in k])
            lampshst = np.array([(fitstbl[k] == '0') | (fitstbl[k] == 'None')
                                    for k in fitstbl.keys() if 'lampshst' in k])
            return np.all(lampstat, axis=0)  # Lamp has to be off
            # return np.all(lampstat | lampshst, axis=0)  # i.e. either the shutter is closed or the lamp is off
        if status == 'arcs':
            # Check if any arc lamps are on (FeAr | ThAr)
            arc_lamp_stat = ['lampstat{0:02d}'.format(i) for i in range(1, 3)]
            arc_lamp_shst = ['lampshst{0:02d}'.format(i) for i in range(1, 3)]
            lamp_stat = np.array([fitstbl[k] == '1' for k in fitstbl.keys()
                                  if k in arc_lamp_stat])
            lamp_shst = np.array([fitstbl[k] == '1' for k in fitstbl.keys()
                                  if k in arc_lamp_shst])
            # Make sure the continuum frames are off
            dome_lamps = ['lampstat{0:02d}'.format(i) for i in range(4, 5)]
            dome_lamp_stat = np.array([fitstbl[k] == '0' for k in fitstbl.keys()
                                       if k in dome_lamps])
            return np.any(lamp_stat & lamp_shst & dome_lamp_stat, axis=0)  # i.e. lamp on and shutter open
        if status in ['dome_noarc', 'dome']:
            # Check if any dome lamps are on (Continuum) - Ignore lampstat03 (Aux) - not sure what this is used for
            dome_lamp_stat = ['lampstat{0:02d}'.format(i) for i in range(4, 5)]
            lamp_stat = np.array([fitstbl[k] == '1' for k in fitstbl.keys()
                                  if k in dome_lamp_stat])
            if status == 'dome_noarc':
                # Make sure arcs are off - it seems even with the shutter closed, the arcs
                arc_lamps = ['lampstat{0:02d}'.format(i) for i in range(1, 3)]
                arc_lamp_stat = np.array([fitstbl[k] == '0' for k in fitstbl.keys()
                                          if k in arc_lamps])
                lamp_stat = lamp_stat & arc_lamp_stat
            return np.any(lamp_stat, axis=0)  # i.e. lamp on
        raise ValueError('No implementation for status = {0}'.format(status))

    def get_lamps_status(self, headarr):
        """
        Return a string containing the information on the lamp status

        Args:
            headarr (list of fits headers):
              list of headers

        Returns:
            str: A string that uniquely represents the lamp status
        """
        # Loop through all lamps and collect their status
        kk = 1
        lampstat = []
        while True:
            lampkey1 = 'lampstat{:02d}'.format(kk)
            if lampkey1 not in self.meta.keys():
                break
            ext1, card1 = self.meta[lampkey1]['ext'], self.meta[lampkey1]['card']
            lampkey2 = 'lampshst{:02d}'.format(kk)
            if lampkey2 not in self.meta.keys():
                lampstat += str(headarr[ext1][card1])
            else:
                ext2, card2 = self.meta[lampkey2]['ext'], self.meta[lampkey2]['card']
                lampstat += str(headarr[ext1][card1]) + '-' + str(headarr[ext2][card2])
            kk += 1
        return "_".join(lampstat)

    def get_rawimage(self, raw_file, det):
        """
        Read a raw KCWI data frame

        Parameters
        ----------
        raw_file : str
          Filename
        det (int or None):
          Detector number
        Returns
        -------
        array : ndarray
          Combined image
        hdu : HDUList
        sections : list
          List of datasec, oscansec, ampsec sections
          datasec, oscansec needs to be for an *unbinned* image as per standard convention
        """
        # Check for file; allow for extra .gz, etc. suffix
        fil = glob.glob(raw_file + '*')
        if len(fil) != 1:
            msgs.error("Found {:d} files matching {:s}".format(len(fil), raw_file))

        # Read
        msgs.info("Reading KCWI file: {:s}".format(fil[0]))
        hdu = fits.open(fil[0])
        head0 = hdu[0].header
        raw_img = hdu[self.detector[det-1]['dataext']].data.astype(float)
        gain_img = self.detector[det-1]['gain']

        # Some properties of the image
        numamps = head0['NVIDINP']
        gainmul, gainarr = head0['GAINMUL'], []
        # Exposure time (used by ProcessRawImage)
        headarr = self.get_headarr(hdu)
        exptime = self.get_meta_value(headarr, 'exptime')

        # get the x and y binning factors...
        binning = head0['BINNING']
        xbin, ybin = [int(ibin) for ibin in binning.split(',')]
        binning_raw = binning

        # Always assume normal FITS header formatting
        one_indexed = True
        include_last = True
        for section in ['DSEC', 'BSEC']:

            # Initialize the image (0 means no amplifier)
            pix_img = np.zeros(raw_img.shape, dtype=int)
            for i in range(numamps):
                # Get the data section
                sec = head0[section+"{0:1d}".format(i+1)]

                # Convert the data section from a string to a slice
                datasec = parse.sec2slice(sec, one_indexed=one_indexed,
                                          include_end=include_last, require_dim=2,
                                          binning=binning_raw)
                # Flip the datasec
                datasec = datasec[::-1]

                if section == 'DSEC':  # Only do this once
                    # Assign the gain for this amplifier
                    gainarr += [head0["GAIN{0:1d}".format(i+1)]*gainmul]

                # Assign the amplifier
                pix_img[datasec] = i+1

            # Finish
            if section == 'DSEC':
                rawdatasec_img = pix_img.copy()
            elif section == 'BSEC':
                oscansec_img = pix_img.copy()

        # Update detector parameters
        self.set_detector_par('gain', det, gainarr, force_update=True)

        # Return
        return raw_img, [head0], exptime, rawdatasec_img, oscansec_img


class KeckKCWIBSpectrograph(KeckKCWISpectrograph):
    """
    Child to handle Keck/KCWI specific code
    """
    def __init__(self):
        # Get it started
        super(KeckKCWISpectrograph, self).__init__()
        self.spectrograph = 'keck_kcwi_blue'
        self.telescope = telescopes.KeckTelescopePar()
        self.camera = 'KCWIb'
        self.detector = [pypeitpar.DetectorPar(
                            dataext         = 0,
                            specaxis        = 0,
                            specflip        = False,
                            xgap            = 0.,
                            ygap            = 0.,
                            ysize           = 1.,
                            platescale      = None,  # <-- TODO : Need to set this
                            darkcurr        = None,  # <-- TODO : Need to set this
                            saturation      = 65535.,
                            nonlinear       = 0.95,       # For lack of a better number!
                            numamplifiers   = 4,          # <-- This is provided in the header
                            gain            = [0]*4,  # <-- This is provided in the header
                            ronoise         = [0]*4,  # <-- TODO : Need to set this
                            datasec         = ['']*4,     # <-- This is provided in the header
                            oscansec        = ['']*4,     # <-- This is provided in the header
                            suffix          = '_01'
                            )]
        self.numhead = 1
        # Uses default timeunit
        # Uses default primary_hdrext
        # self.sky_file ?

        # Don't instantiate these until they're needed
        self.grating = None
        self.optical_model = None
        self.detector_map = None

    def default_pypeit_par(self):
        """
        Set default parameters for Keck KCWI reductions.
        """
        par = pypeitpar.PypeItPar()
        par['rdx']['spectrograph'] = 'keck_kcwi_blue'
        #par['flexure']['method'] = 'boxcar'
        # Set wave tilts order

        # Set the slit edge parameters
        par['calibrations']['slitedges']['fit_order'] = 4

        # 1D wavelength solution
        #par['calibrations']['wavelengths']['lamps'] = ['ArI','NeI','KrI','XeI']
        #par['calibrations']['wavelengths']['nonlinear_counts'] \
        #        = self.detector[0]['nonlinear'] * self.detector[0]['saturation']
        #par['calibrations']['wavelengths']['n_first'] = 3
        #par['calibrations']['wavelengths']['match_toler'] = 2.5

        # Alter the method used to combine pixel flats
        par['calibrations']['pixelflatframe']['process']['combine'] = 'median'
        par['calibrations']['pixelflatframe']['process']['sig_lohi'] = [10.,10.]

        # Set the default exposure time ranges for the frame typing
        par['calibrations']['biasframe']['exprng'] = [None, 0.01]
        par['calibrations']['darkframe']['exprng'] = [0.01, None]
        par['calibrations']['pinholeframe']['exprng'] = [999999, None]  # No pinhole frames
        par['calibrations']['pixelflatframe']['exprng'] = [None, 30]
        par['calibrations']['traceframe']['exprng'] = [None, 30]
        par['scienceframe']['exprng'] = [30, None]
        
        # LACosmics parameters
        par['scienceframe']['process']['sigclip'] = 4.0
        par['scienceframe']['process']['objlim'] = 1.5

        return par

    def config_specific_par(self, scifile, inp_par=None):
        """
        Modify the PypeIt parameters to hard-wired values used for
        specific instrument configurations.

        .. todo::
            Document the changes made!
        
        Args:
            scifile (str):
                File to use when determining the configuration and how
                to adjust the input parameters.
            inp_par (:class:`pypeit.par.parset.ParSet`, optional):
                Parameter set used for the full run of PypeIt.  If None,
                use :func:`default_pypeit_par`.

        Returns:
            :class:`pypeit.par.parset.ParSet`: The PypeIt paramter set
            adjusted for configuration specific parameter values.
        """
        par = self.default_pypeit_par() if inp_par is None else inp_par

        headarr = self.get_headarr(scifile)

        # Templates
        if self.get_meta_value(headarr, 'dispname') == 'BH2':
            par['calibrations']['wavelengths']['method'] = 'identify'#'full_template'
            par['calibrations']['wavelengths']['reid_arxiv'] = ''
            #par['calibrations']['wavelengths']['lamps'] = ['ThAr']
        if self.get_meta_value(headarr, 'dispname') == 'BM':
            par['calibrations']['wavelengths']['method'] = 'identify'#'full_template'
            par['calibrations']['wavelengths']['reid_arxiv'] = ''
            #par['calibrations']['wavelengths']['lamps'] = ['ThAr']

        # FWHM
        #binning = parse.parse_binning(self.get_meta_value(headarr, 'binning'))
        #par['calibrations']['wavelengths']['fwhm'] = 6.0 / binning[1]

        # Return
        return par

    def bpm(self, filename, det, shape=None, msbias=None):
        """
        Override parent bpm function with BPM specific to DEIMOS.

        Parameters
        ----------
        det : int, REQUIRED
        **null_kwargs:
            Captured and never used

        Returns
        -------
        bpix : ndarray
          0 = ok; 1 = Mask

        """
        bpm_img = self.empty_bpm(filename, det, shape=shape)

        # Fill in bad pixels if a master bias frame is provided
        if msbias is not None:
            return self.bpm_frombias(msbias, det, bpm_img)

        # Extract some header info
        #msgs.info("Reading AMPMODE and BINNING from KCWI file: {:s}".format(filename))
        head0 = fits.getheader(filename, ext=0)
        ampmode = head0['AMPMODE']
        binning = head0['BINNING']

        # Construct a list of the bad columns
        # Note: These were taken from v1.1.0 (REL) Date: 2018/06/11 of KDERP
        #       KDERP store values and in the code (stage1) subtract 1 from the badcol data files.
        #       Instead of this, I have already pre-subtracted the values in the following arrays.
        bc = None
        if ampmode == 'ALL':
            if binning == '1,1':
                bc = [[3676, 3676, 2056, 2244]]
            elif binning == '2,2':
                bc = [[1838, 1838, 1028, 1121]]
        elif ampmode == 'TBO':
            if binning == '1,1':
                bc = [[2622, 2622,  619,  687],
                      [2739, 2739, 1748, 1860],
                      [3295, 3300, 2556, 2560],
                      [3675, 3676, 2243, 4111]]
            elif binning == '2,2':
                bc = [[1311, 1311,  310,  354],
                      [1369, 1369,  876,  947],
                      [1646, 1650, 1278, 1280],
                      [1838, 1838, 1122, 2055]]
        if ampmode == 'TUP':
            if binning == '1,1':
                bc = [[2622, 2622, 3492, 3528],
                      [3295, 3300, 1550, 1555],
                      [3676, 3676, 1866, 4111]]
            elif binning == '2,2':
                bc = [[1311, 1311, 1745, 1788],
                      [1646, 1650,  775,  777],
                      [1838, 1838,  933, 2055]]
        if bc is None:
            msgs.warn("Bad pixel mask is not available for ampmode={0:s} binning={1:s}".format(ampmode, binning))
            bc = []

        # Apply these bad columns to the mask
        for bb in range(len(bc)):
            bpm_img[bc[bb][2]:bc[bb][3]+1, bc[bb][0]:bc[bb][1]+1] = 1

        return bpm_img