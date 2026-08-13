use scripting additions

on replace_text(find_text, replacement_text, source_text)
	set previous_delimiters to AppleScript's text item delimiters
	set AppleScript's text item delimiters to find_text
	set source_items to every text item of source_text
	set AppleScript's text item delimiters to replacement_text
	set output_text to source_items as text
	set AppleScript's text item delimiters to previous_delimiters
	return output_text
end replace_text

on clean_field(field_value)
	if field_value is missing value then return ""
	set unit_separator to character id 31
	set record_separator to character id 30
	set field_text to field_value as text
	set field_text to my replace_text(unit_separator, " ", field_text)
	set field_text to my replace_text(record_separator, " ", field_text)
	return field_text
end clean_field

on scan_library()
	set unit_separator to character id 31
	set record_separator to character id 30
	set output_rows to {}
	tell application "Music"
		launch
		-- File tracks have a local audio file. This deliberately excludes the
		-- Apple Music streaming/cloud catalog.
		-- Fetch only the eight required fields as aligned lists. This avoids
		-- tens of thousands of per-track Apple events and avoids materializing
		-- every unused Music property.
		set track_pids to persistent ID of every file track of library playlist 1
		set track_names to name of every file track of library playlist 1
		set track_artists to artist of every file track of library playlist 1
		set track_albums to album of every file track of library playlist 1
		set track_durations to duration of every file track of library playlist 1
		set track_enabled_values to enabled of every file track of library playlist 1
		set track_comments to comment of every file track of library playlist 1
		set track_locations to location of every file track of library playlist 1
		repeat with track_index from 1 to count of track_pids
			try
				set track_pid to item track_index of track_pids
				set track_name to item track_index of track_names
				set track_artist to item track_index of track_artists
				set track_album to item track_index of track_albums
				set track_duration to item track_index of track_durations
				set track_enabled to item track_index of track_enabled_values
				set track_comment to item track_index of track_comments
				set track_location to ""
				try
					set track_location to POSIX path of (item track_index of track_locations)
				end try
				set row_fields to {my clean_field(track_pid), my clean_field(track_name), my clean_field(track_artist), my clean_field(track_album), my clean_field(track_duration), my clean_field(track_enabled), my clean_field(track_location), my clean_field(track_comment)}
				set previous_delimiters to AppleScript's text item delimiters
				set AppleScript's text item delimiters to unit_separator
				set end of output_rows to row_fields as text
				set AppleScript's text item delimiters to previous_delimiters
			end try
		end repeat
	end tell
	set previous_delimiters to AppleScript's text item delimiters
	set AppleScript's text item delimiters to record_separator
	set output_text to output_rows as text
	set AppleScript's text item delimiters to previous_delimiters
	return output_text
end scan_library

on prefer_track(argv)
	set new_path to item 2 of argv
	set spotify_id to item 3 of argv
	set playlist_name to item 4 of argv
	set new_name to item 5 of argv
	set new_artist to item 6 of argv
	set new_album to item 7 of argv
	set new_track_number to item 8 of argv as integer
	set new_track_count to item 9 of argv as integer
	set new_disc_number to item 10 of argv as integer
	set new_compilation to my boolean_argument(item 11 of argv)
	set existing_path_pid to item 12 of argv
	set new_file to POSIX file new_path
	set marker_text to "SPOTIFY_ARCHIVE_ID=" & spotify_id
	set preferred_track to missing value
	set preferred_existed_before to false
	set preferred_enabled_before to false
	set playlist_had_track to false

	tell application "Music"
		launch

		-- Python scans Music once and supplies the persistent ID when this file
		-- is already present. Avoid rescanning every file track for every song.
		if existing_path_pid is not "" then
			set path_matches to every track of library playlist 1 whose persistent ID is existing_path_pid
			if (count of path_matches) > 0 then
				set preferred_track to item 1 of path_matches
				set preferred_existed_before to true
			end if
		end if

		if preferred_track is missing value then
			set added_result to add new_file
			if class of added_result is list then
				set preferred_track to item 1 of added_result
			else
				set preferred_track to added_result
			end if
		end if
		if preferred_existed_before then
			try
				set preferred_enabled_before to enabled of preferred_track
			end try
		end if

		set metadata_ready to false
		repeat 12 times
			try
				set name of preferred_track to new_name
				set artist of preferred_track to new_artist
				set album of preferred_track to new_album
				if new_track_number > 0 then set track number of preferred_track to new_track_number
				if new_track_count > 0 then set track count of preferred_track to new_track_count
				if new_disc_number > 0 then set disc number of preferred_track to new_disc_number
				set compilation of preferred_track to new_compilation
				set comment of preferred_track to marker_text
				set enabled of preferred_track to true
				set metadata_ready to true
				exit repeat
			on error
				delay 1
			end try
		end repeat
		if metadata_ready is false then error "Music did not finish importing the preferred file."

		set preferred_pid to persistent ID of preferred_track

		if not (exists user playlist playlist_name) then
			make new user playlist with properties {name:playlist_name}
		end if
		set target_playlist to user playlist playlist_name
		set playlist_matches to every track of target_playlist whose persistent ID is preferred_pid
		if (count of playlist_matches) > 0 then
			set playlist_had_track to true
		else
			duplicate preferred_track to target_playlist
		end if

		if (count of argv) > 12 then
			repeat with argument_index from 13 to count of argv
				set old_pid to item argument_index of argv
				if old_pid is not preferred_pid then
					set old_matches to every track of library playlist 1 whose persistent ID is old_pid
					repeat with old_track in old_matches
						try
							set old_album to album of old_track as text
							ignoring case
								if old_album does not contain "(VINYL)" then set enabled of old_track to false
							end ignoring
						end try
					end repeat
				end if
			end repeat
		end if

		set preferred_location to ""
		try
			set preferred_location to POSIX path of (location of preferred_track)
		end try
		return preferred_pid & (character id 31) & preferred_location & (character id 31) & preferred_existed_before & (character id 31) & preferred_enabled_before & (character id 31) & playlist_had_track
	end tell
end prefer_track

on boolean_argument(argument_value)
	return (argument_value as text) is "true"
end boolean_argument

on playlist_ids(argv)
	set playlist_name to item 2 of argv
	tell application "Music"
		launch
		if not (exists user playlist playlist_name) then error "Music playlist not found: " & playlist_name
		set playlist_pids to {}
		try
			set playlist_pids to persistent ID of every track of user playlist playlist_name
		end try
	end tell
	set previous_delimiters to AppleScript's text item delimiters
	set AppleScript's text item delimiters to character id 31
	set output_text to playlist_pids as text
	set AppleScript's text item delimiters to previous_delimiters
	return output_text
end playlist_ids

on playlist_add(argv)
	set playlist_name to item 2 of argv
	set requested_count to (count of argv) - 2
	set added_count to 0
	set missing_count to 0
	tell application "Music"
		launch
		if not (exists user playlist playlist_name) then
			make new user playlist with properties {name:playlist_name}
		end if
		set target_playlist to user playlist playlist_name
		set target_pids to {}
		try
			set target_pids to persistent ID of every track of target_playlist
		end try
		set previous_count to count of target_pids
		if requested_count > 0 then
			repeat with argument_index from 3 to count of argv
				set requested_pid to item argument_index of argv
				if target_pids does not contain requested_pid then
					set library_matches to every track of library playlist 1 whose persistent ID is requested_pid
					if (count of library_matches) > 0 then
						duplicate item 1 of library_matches to target_playlist
						set end of target_pids to requested_pid
						set added_count to added_count + 1
					else
						set missing_count to missing_count + 1
					end if
				end if
			end repeat
		end if
		set final_count to count of every track of target_playlist
	end tell
	return (requested_count as text) & (character id 31) & (previous_count as text) & (character id 31) & (added_count as text) & (character id 31) & (missing_count as text) & (character id 31) & (final_count as text)
end playlist_add

on playlist_state(argv)
	set playlist_name to item 2 of argv
	set playlist_exists to false
	set playlist_pids to {}
	tell application "Music"
		launch
		if exists user playlist playlist_name then
			set playlist_exists to true
			try
				set playlist_pids to persistent ID of every track of user playlist playlist_name
			end try
		end if
	end tell
	set previous_delimiters to AppleScript's text item delimiters
	set AppleScript's text item delimiters to character id 30
	set pid_text to playlist_pids as text
	set AppleScript's text item delimiters to previous_delimiters
	return (playlist_exists as text) & (character id 31) & pid_text
end playlist_state

on playlist_append(argv)
	set playlist_name to item 2 of argv
	set requested_count to (count of argv) - 2
	set added_count to 0
	set missing_count to 0
	tell application "Music"
		launch
		if not (exists user playlist playlist_name) then
			make new user playlist with properties {name:playlist_name}
		end if
		set target_playlist to user playlist playlist_name
		if requested_count > 0 then
			repeat with argument_index from 3 to count of argv
				set requested_pid to item argument_index of argv
				set library_matches to every track of library playlist 1 whose persistent ID is requested_pid
				if (count of library_matches) > 0 then
					set source_track to item 1 of library_matches
					try
						duplicate source_track to target_playlist
						set added_count to added_count + 1
					on error
						-- Music can refuse a duplicate-equivalent library entry.
						-- Skip only that item so later playlist songs still run.
						set missing_count to missing_count + 1
					end try
				else
					set missing_count to missing_count + 1
				end if
			end repeat
		end if
		set final_count to count of every track of target_playlist
	end tell
	return (requested_count as text) & (character id 31) & (added_count as text) & (character id 31) & (missing_count as text) & (character id 31) & (final_count as text)
end playlist_append

on playlist_rebuild(argv)
	set playlist_name to item 2 of argv
	set backup_name to item 3 of argv
	set requested_count to (count of argv) - 3
	set added_count to 0
	set missing_count to 0
	set backup_used to ""
	tell application "Music"
		launch
		if exists user playlist playlist_name then
			set old_playlist to user playlist playlist_name
			set name of old_playlist to backup_name
			set backup_used to backup_name
		end if
		set target_playlist to make new user playlist with properties {name:playlist_name}
		if requested_count > 0 then
			repeat with argument_index from 4 to count of argv
				set requested_pid to item argument_index of argv
				set library_matches to every track of library playlist 1 whose persistent ID is requested_pid
				if (count of library_matches) > 0 then
					try
						duplicate item 1 of library_matches to target_playlist
						set added_count to added_count + 1
					on error
						set missing_count to missing_count + 1
					end try
				else
					set missing_count to missing_count + 1
				end if
			end repeat
		end if
		set final_count to count of every track of target_playlist
	end tell
	return (requested_count as text) & (character id 31) & (added_count as text) & (character id 31) & (missing_count as text) & (character id 31) & (final_count as text) & (character id 31) & backup_used
end playlist_rebuild

on genre_scan()
	set unit_separator to character id 31
	set record_separator to character id 30
	set output_rows to {}
	tell application "Music"
		launch
		set track_pids to persistent ID of every file track of library playlist 1
		set track_names to name of every file track of library playlist 1
		set track_artists to artist of every file track of library playlist 1
		set track_album_artists to album artist of every file track of library playlist 1
		set track_albums to album of every file track of library playlist 1
		set track_durations to duration of every file track of library playlist 1
		set track_enabled_values to enabled of every file track of library playlist 1
		set track_locations to location of every file track of library playlist 1
		set track_comments to comment of every file track of library playlist 1
		set track_genres to genre of every file track of library playlist 1
		repeat with track_index from 1 to count of track_pids
			try
				set track_location to ""
				try
					set track_location to POSIX path of (item track_index of track_locations)
				end try
				set row_fields to {my clean_field(item track_index of track_pids), my clean_field(item track_index of track_names), my clean_field(item track_index of track_artists), my clean_field(item track_index of track_album_artists), my clean_field(item track_index of track_albums), my clean_field(item track_index of track_durations), my clean_field(item track_index of track_enabled_values), my clean_field(track_location), my clean_field(item track_index of track_comments), my clean_field(item track_index of track_genres)}
				set previous_delimiters to AppleScript's text item delimiters
				set AppleScript's text item delimiters to unit_separator
				set end of output_rows to row_fields as text
				set AppleScript's text item delimiters to previous_delimiters
			end try
		end repeat
	end tell
	set previous_delimiters to AppleScript's text item delimiters
	set AppleScript's text item delimiters to record_separator
	set output_text to output_rows as text
	set AppleScript's text item delimiters to previous_delimiters
	return output_text
end genre_scan

on genre_set(argv)
	set applied_count to 0
	set missing_count to 0
	set protected_vinyl_count to 0
	tell application "Music"
		launch
		if (count of argv) > 1 then
			repeat with argument_index from 2 to count of argv by 2
				if argument_index + 1 is less than or equal to count of argv then
					set requested_pid to item argument_index of argv
					set requested_genre to item (argument_index + 1) of argv
					set library_matches to every track of library playlist 1 whose persistent ID is requested_pid
					if (count of library_matches) = 0 then
						set missing_count to missing_count + 1
					else
						set target_track to item 1 of library_matches
						set target_album to album of target_track as text
						ignoring case
							if target_album contains "(VINYL)" then
								set protected_vinyl_count to protected_vinyl_count + 1
							else
								set genre of target_track to requested_genre
								set applied_count to applied_count + 1
							end if
						end ignoring
					end if
				end if
			end repeat
		end if
	end tell
	return (applied_count as text) & (character id 31) & (missing_count as text) & (character id 31) & (protected_vinyl_count as text)
end genre_set

on metadata_scan()
	set unit_separator to character id 31
	set record_separator to character id 30
	set output_rows to {}
	tell application "Music"
		launch
		set track_pids to persistent ID of every file track of library playlist 1
		set track_names to name of every file track of library playlist 1
		set track_artists to artist of every file track of library playlist 1
		set track_album_artists to album artist of every file track of library playlist 1
		set track_albums to album of every file track of library playlist 1
		set track_genres to genre of every file track of library playlist 1
		set track_years to year of every file track of library playlist 1
		set track_numbers to track number of every file track of library playlist 1
		set track_counts to track count of every file track of library playlist 1
		set track_disc_numbers to disc number of every file track of library playlist 1
		set track_compilations to compilation of every file track of library playlist 1
		set track_comments to comment of every file track of library playlist 1
		set track_enabled_values to enabled of every file track of library playlist 1
		set track_locations to location of every file track of library playlist 1
		repeat with track_index from 1 to count of track_pids
			try
				set track_location to ""
				try
					set track_location to POSIX path of (item track_index of track_locations)
				end try
				set row_fields to {my clean_field(item track_index of track_pids), my clean_field(item track_index of track_names), my clean_field(item track_index of track_artists), my clean_field(item track_index of track_album_artists), my clean_field(item track_index of track_albums), my clean_field(item track_index of track_genres), my clean_field(item track_index of track_years), my clean_field(item track_index of track_numbers), my clean_field(item track_index of track_counts), my clean_field(item track_index of track_disc_numbers), my clean_field(item track_index of track_compilations), my clean_field(item track_index of track_comments), my clean_field(item track_index of track_enabled_values), my clean_field(track_location)}
				set previous_delimiters to AppleScript's text item delimiters
				set AppleScript's text item delimiters to unit_separator
				set end of output_rows to row_fields as text
				set AppleScript's text item delimiters to previous_delimiters
			end try
		end repeat
	end tell
	set previous_delimiters to AppleScript's text item delimiters
	set AppleScript's text item delimiters to record_separator
	set output_text to output_rows as text
	set AppleScript's text item delimiters to previous_delimiters
	return output_text
end metadata_scan

on metadata_set(argv)
	set applied_count to 0
	set missing_count to 0
	set protected_vinyl_count to 0
	set fields_per_track to 12
	tell application "Music"
		launch
		if (count of argv) > 1 then
			repeat with argument_index from 2 to count of argv by fields_per_track
				if argument_index + fields_per_track - 1 is less than or equal to count of argv then
					set requested_pid to item argument_index of argv
					set requested_name to item (argument_index + 1) of argv
					set requested_artist to item (argument_index + 2) of argv
					set requested_album_artist to item (argument_index + 3) of argv
					set requested_album to item (argument_index + 4) of argv
					set requested_genre to item (argument_index + 5) of argv
					set requested_year to item (argument_index + 6) of argv as integer
					set requested_track_number to item (argument_index + 7) of argv as integer
					set requested_track_count to item (argument_index + 8) of argv as integer
					set requested_disc_number to item (argument_index + 9) of argv as integer
					set requested_compilation to my boolean_argument(item (argument_index + 10) of argv)
					set requested_comment to item (argument_index + 11) of argv
					set library_matches to every track of library playlist 1 whose persistent ID is requested_pid
					if (count of library_matches) = 0 then
						set missing_count to missing_count + 1
					else
						set target_track to item 1 of library_matches
						set target_album to album of target_track as text
						ignoring case
							if target_album contains "(VINYL)" then
								set protected_vinyl_count to protected_vinyl_count + 1
							else
								set name of target_track to requested_name
								set artist of target_track to requested_artist
								set album artist of target_track to requested_album_artist
								set album of target_track to requested_album
								set genre of target_track to requested_genre
								if requested_year > 0 then set year of target_track to requested_year
								if requested_track_number > 0 then set track number of target_track to requested_track_number
								if requested_track_count > 0 then set track count of target_track to requested_track_count
								if requested_disc_number > 0 then set disc number of target_track to requested_disc_number
								set compilation of target_track to requested_compilation
								set comment of target_track to requested_comment
								set applied_count to applied_count + 1
							end if
						end ignoring
					end if
				end if
			end repeat
		end if
	end tell
	return (applied_count as text) & (character id 31) & (missing_count as text) & (character id 31) & (protected_vinyl_count as text)
end metadata_set

on delete_library_track(argv)
	set requested_pid to item 2 of argv
	tell application "Music"
		launch
		set library_matches to every track of library playlist 1 whose persistent ID is requested_pid
		if (count of library_matches) = 0 then return "missing"
		set target_track to item 1 of library_matches
		set target_album to album of target_track as text
		ignoring case
			if target_album contains "(VINYL)" then return "protected_vinyl"
		end ignoring
		delete target_track
	end tell
	return "deleted"
end delete_library_track

on trash_file(argv)
	set requested_path to item 2 of argv
	set target_file to POSIX file requested_path
	tell application "Finder"
		if not (exists target_file) then return "missing"
		delete target_file
	end tell
	return "trashed"
end trash_file

on album_artist_set(argv)
	set applied_count to 0
	set missing_count to 0
	set protected_vinyl_count to 0
	tell application "Music"
		launch
		if (count of argv) > 1 then
			repeat with argument_index from 2 to count of argv by 3
				if argument_index + 2 is less than or equal to count of argv then
					set requested_pid to item argument_index of argv
					set requested_album_artist to item (argument_index + 1) of argv
					set requested_compilation to my boolean_argument(item (argument_index + 2) of argv)
					set library_matches to every track of library playlist 1 whose persistent ID is requested_pid
					if (count of library_matches) = 0 then
						set missing_count to missing_count + 1
					else
						set target_track to item 1 of library_matches
						set target_album to album of target_track as text
						ignoring case
							if target_album contains "(VINYL)" then
								set protected_vinyl_count to protected_vinyl_count + 1
							else
								set album artist of target_track to requested_album_artist
								set compilation of target_track to requested_compilation
								set applied_count to applied_count + 1
							end if
						end ignoring
					end if
				end if
			end repeat
		end if
	end tell
	return (applied_count as text) & (character id 31) & (missing_count as text) & (character id 31) & (protected_vinyl_count as text)
end album_artist_set

on album_group_set(argv)
	set applied_count to 0
	set missing_count to 0
	set protected_vinyl_count to 0
	tell application "Music"
		launch
		if (count of argv) > 1 then
			repeat with argument_index from 2 to count of argv by 4
				if argument_index + 3 is less than or equal to count of argv then
					set requested_pid to item argument_index of argv
					set requested_album to item (argument_index + 1) of argv
					set requested_album_artist to item (argument_index + 2) of argv
					set requested_compilation to my boolean_argument(item (argument_index + 3) of argv)
					set library_matches to every track of library playlist 1 whose persistent ID is requested_pid
					if (count of library_matches) = 0 then
						set missing_count to missing_count + 1
					else
						set target_track to item 1 of library_matches
						set target_album to album of target_track as text
						ignoring case
							if target_album contains "(VINYL)" then
								set protected_vinyl_count to protected_vinyl_count + 1
							else
								set album of target_track to requested_album
								set album artist of target_track to requested_album_artist
								set compilation of target_track to requested_compilation
								set applied_count to applied_count + 1
							end if
						end ignoring
					end if
				end if
			end repeat
		end if
	end tell
	return (applied_count as text) & (character id 31) & (missing_count as text) & (character id 31) & (protected_vinyl_count as text)
end album_group_set

on restore_track(argv)
	set preferred_pid to item 2 of argv
	set preferred_existed_before to my boolean_argument(item 3 of argv)
	set preferred_enabled_before to my boolean_argument(item 4 of argv)
	set playlist_name to item 5 of argv
	set playlist_had_track to my boolean_argument(item 6 of argv)

	tell application "Music"
		launch
		set preferred_matches to every track of library playlist 1 whose persistent ID is preferred_pid
		repeat with preferred_track in preferred_matches
			if preferred_existed_before then
				set enabled of preferred_track to preferred_enabled_before
			else
				-- A newly imported library entry is kept but disabled.
				set enabled of preferred_track to false
			end if
		end repeat

		if playlist_had_track is false and (exists user playlist playlist_name) then
			set target_playlist to user playlist playlist_name
			set playlist_matches to every track of target_playlist whose persistent ID is preferred_pid
			repeat with playlist_track in playlist_matches
				-- This removes only the playlist reference, never the library entry.
				delete playlist_track
			end repeat
		end if

		if (count of argv) > 6 then
			repeat with argument_index from 7 to count of argv by 2
				if argument_index + 1 is less than or equal to count of argv then
					set old_pid to item argument_index of argv
					set old_enabled to my boolean_argument(item (argument_index + 1) of argv)
					set old_matches to every track of library playlist 1 whose persistent ID is old_pid
					repeat with old_track in old_matches
						set enabled of old_track to old_enabled
					end repeat
				end if
			end repeat
		end if
		return "restored"
	end tell
end restore_track

on run argv
	if (count of argv) is 0 then error "A command is required."
	set command_name to item 1 of argv
	if command_name is "scan" then
		return my scan_library()
	else if command_name is "prefer" then
		return my prefer_track(argv)
	else if command_name is "restore" then
		return my restore_track(argv)
	else if command_name is "playlist-ids" then
		return my playlist_ids(argv)
	else if command_name is "playlist-add" then
		return my playlist_add(argv)
	else if command_name is "playlist-state" then
		return my playlist_state(argv)
	else if command_name is "playlist-append" then
		return my playlist_append(argv)
	else if command_name is "playlist-rebuild" then
		return my playlist_rebuild(argv)
	else if command_name is "genre-scan" then
		return my genre_scan()
	else if command_name is "genre-set" then
		return my genre_set(argv)
	else if command_name is "metadata-scan" then
		return my metadata_scan()
	else if command_name is "metadata-set" then
		return my metadata_set(argv)
	else if command_name is "delete-library-track" then
		return my delete_library_track(argv)
	else if command_name is "trash-file" then
		return my trash_file(argv)
	else if command_name is "album-artist-set" then
		return my album_artist_set(argv)
	else if command_name is "album-group-set" then
		return my album_group_set(argv)
	else
		error "Unknown command: " & command_name
	end if
end run
